"""Parses the four wire formats (sessions/readiness/scale weigh-ins'
Markdown-with-YAML-frontmatter, and ECG's binary format) into their
respective Parsed* dataclasses.

These are the *one* parsing function per record type, shared by the live
POST handlers (app/routes.py) and the standalone reparse.py script — per the
spec, a given record type's parsing logic must never diverge between the two
callers. Everything here is pure (bytes in, a Parsed* dataclass out, or an
exception) and touches neither the network nor the database, so it's
trivially testable and safe to re-run offline against files already on disk.

Field rules (see docs/sync-ingestion/SPEC.md, READINESS_SPEC.md,
SCALE_SPEC.md, and ECG_SPEC.md "Field notes"/"Binary format" sections for
the source of truth):
- Any numeric field can be entirely absent from the YAML when its value is 0
  — treat missing as 0/0.0, never as an error or None.
- `tonnageByBodypart` entries with value 0 are dropped entirely rather than
  written as 0.0 — so only keys actually present matter.
- `completedAt` may be `""` (abandoned session) — store as-is, not an error.
- `excludeFromTonnage` / `isDaily` are omitted when false — missing means False.
- Unrecognized top-level YAML keys are ignored, not an error — the phone app
  evolves independently and may add fields these parsers don't know about yet.
- `sets[].isBodyweight`: same "omitted means False" convention as the other
  booleans above. See ANALYSIS_SPEC.md's common_tonnage note for why this
  distinction matters (an untouched/empty set also has weight=0). The phone
  now sends it, together with a materialized `weight` (see next) and the
  `bwLoadPercent`/`bwBaseWeightKg` audit pair.
- `sets[].bwLoadPercent` / `sets[].bwBaseWeightKg` (ANALYSIS_SPEC.md's
  isBodyweight note): audit fields the phone attaches to a bodyweight set
  alongside a now-materialized `weight` (= bwLoadPercent/100 * body weight at
  session date, rounded to 0.5 kg; stays 0.0 only when no weigh-in was
  available). `bwLoadPercent` is one of 25/50/75/100 — the movement's
  bodyweight fraction; `bwBaseWeightKg` is the body weight used. Both stay
  `None` when absent (they only appear on bodyweight sets, and older synced
  files predate them) — no server math reads them, they exist to explain a
  set's weight in the detail view. `weight` itself now flows through the
  existing tonnage/PR/e1RM paths unchanged, since it's a real positive number.
- `sessionRpe`/`sessionLoad` (ANALYSIS_SPEC.md §13): unlike the space-saving
  "missing means 0" numeric fields above, these stay `None` when absent —
  0 is not a valid RPE value, so "not answered" must be distinguishable from
  a reported effort. Use `_optional_int`/`_optional_num`, not `_int`/`_num`.
- `exercises[].type == "CARDIO"` (docs/sync-ingestion/CARDIO_BLOCKS_SPEC.md):
  a set is `{startedAt, endedAt, avgHr, maxHr}` instead of `{reps, weight}`
  (FORZA) or `{timeSeconds, done}` (STRETCH). `reps`/`weight` stay `None` on
  these rows and vice versa — `started_at`/`ended_at`/`avg_hr`/`max_hr` stay
  `None` on non-CARDIO rows. Unlike the "missing means 0/''" convention
  above, blank/missing here means "not applicable" or "block still running/
  abandoned," so it maps to `None`, not `0`/`""`.
- `exercises[].type == "STRETCH"`: a set is `{timeSeconds, done}` — the held
  duration in seconds. `time_seconds` stays `None` on FORZA/CARDIO rows and
  `reps`/`weight` stay `None` on STRETCH rows. Consumed by
  app.analysis.stretch_weekly_trend (weekly held-seconds per muscle group).
  A missing/blank `timeSeconds` on a stretch set means the set was
  untouched/abandoned, so it maps to `None`, not `0` — same reasoning as the
  CARDIO fields above.
- `exercises[].type` is matched case-insensitively against `CARDIO` — real
  synced data has been observed sending lowercase (`cardio`/`forza`/
  `stretch`), not SPEC.md's documented uppercase. A case-sensitive match
  here would silently misparse every real CARDIO block as FORZA (reps/
  weight populated as 0, HR/timing fields lost) rather than erroring, which
  is exactly the kind of silent data loss the raw-first design exists to
  avoid at the *storage* layer — this fixes the same failure mode in the
  *parse* layer, where nothing forces it to be caught otherwise.
"""

import struct

import yaml

from app.storage import (
    ExerciseSet,
    ParsedEcg,
    ParsedExercise,
    ParsedReadiness,
    ParsedRoutine,
    ParsedScaleWeighin,
    ParsedSession,
    RoutineExercise,
)


class SessionParseError(ValueError):
    """Raised when a file doesn't look like a valid record of its claimed
    type (bad frontmatter, missing required identity fields, etc). Callers
    (routes.py, reparse.py) catch this — per the raw-first principle, a
    parse failure must never be allowed to take down the raw write or crash
    the reparse batch. Named for the original (session) use case but shared
    across all three record-type parsers below."""


_ID_KEYS = ("id", "routineId", "exerciseId")


class _StringIdLoader(yaml.SafeLoader):
    """A hex id/exerciseId/routineId can be all-digits (e.g. "00000001"),
    which plain yaml.safe_load would parse as the *int* 1 — silently
    dropping the leading zeros and corrupting the id. These three keys are
    always strings on the wire (see spec's ID format notes), so force the
    value node under them to construct as a plain string *before* the
    default int/float resolver ever sees it — doing this after construction
    is too late, since the leading zeros are already gone by then."""


def _construct_mapping_with_string_ids(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in _ID_KEYS and isinstance(value_node, yaml.ScalarNode):
            value = loader.construct_scalar(value_node)
        else:
            value = loader.construct_object(value_node, deep=True)
        mapping[key] = value
    return mapping


_StringIdLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_with_string_ids
)

# All date/datetime-shaped fields (date, startedAt, completedAt) are stored
# as TEXT in SQLite verbatim, not converted to Python date/datetime objects —
# PyYAML's default implicit timestamp resolver would auto-convert them,
# which both loses the exact on-the-wire formatting and trips sqlite3's
# deprecated-adapter warning on insert. Drop that implicit resolver so these
# stay plain strings end to end.
_StringIdLoader.yaml_implicit_resolvers = {
    first_char: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def parse_session_file(raw_bytes: bytes) -> ParsedSession:
    text = raw_bytes.decode("utf-8")
    frontmatter, body = _split_frontmatter(text)

    try:
        data = yaml.load(frontmatter, Loader=_StringIdLoader) or {}
    except yaml.YAMLError as exc:
        raise SessionParseError(f"invalid YAML frontmatter: {exc}") from exc

    if not isinstance(data, dict):
        raise SessionParseError("frontmatter did not parse to a mapping")

    session_id = data.get("id")
    if not session_id or not isinstance(session_id, str):
        raise SessionParseError("frontmatter missing required 'id' field")

    tonnage_by_bodypart = {
        str(bodypart): round(float(tonnage), 2)
        for bodypart, tonnage in (data.get("tonnageByBodypart") or {}).items()
    }

    exercise_sets = _parse_exercises(data.get("exercises") or [])

    return ParsedSession(
        id=session_id,
        date=data.get("date"),
        routine_id=data.get("routineId"),
        routine_name=data.get("routineName"),
        started_at=data.get("startedAt"),
        # completedAt may legitimately be "" (abandoned session) — never None.
        completed_at=data.get("completedAt") or "",
        total_tonnage=_num(data, "totalTonnage"),
        session_calories=_num(data, "sessionCalories"),
        session_trimp=_num(data, "sessionTrimp"),
        vo2max=_num(data, "vo2max"),
        ecg_beats=_int(data, "ecgBeats"),
        ecg_duration_sec=_int(data, "ecgDurationSec"),
        ecg_session_rmssd=_num(data, "ecgSessionRmssd"),
        ecg_pac_count=_int(data, "ecgPacCount"),
        ecg_pause_count=_int(data, "ecgPauseCount"),
        ecg_irregular_beats=_int(data, "ecgIrregularBeats"),
        afib_suspicion_episodes=_int(data, "afibSuspicionEpisodes"),
        sdnn=_num(data, "sdnn"),
        pnn50=_num(data, "pnn50"),
        poincare_sd1=_num(data, "poincareSd1"),
        poincare_sd2=_num(data, "poincareSd2"),
        poincare_ratio=_num(data, "poincareRatio"),
        cardiac_drift_bpm_min=_num(data, "cardiacDriftBpmMin"),
        resting_hr=_int(data, "restingHr"),
        hrr60s=_int(data, "hrr60s"),
        session_rpe=_optional_int(data, "sessionRpe"),
        session_load=_optional_num(data, "sessionLoad"),
        notes=body.strip(),
        tonnage_by_bodypart=tonnage_by_bodypart,
        exercise_sets=exercise_sets,
    )


def parse_readiness_file(raw_bytes: bytes) -> ParsedReadiness:
    """See docs/sync-ingestion/READINESS_SPEC.md. `readiness` events are a
    single flat record (no child tables, no free-text body to speak of)."""
    text = raw_bytes.decode("utf-8")
    frontmatter, _body = _split_frontmatter(text)

    try:
        data = yaml.load(frontmatter, Loader=_StringIdLoader) or {}
    except yaml.YAMLError as exc:
        raise SessionParseError(f"invalid YAML frontmatter: {exc}") from exc

    if not isinstance(data, dict):
        raise SessionParseError("frontmatter did not parse to a mapping")

    event_id = data.get("id")
    if not event_id or not isinstance(event_id, str):
        raise SessionParseError("frontmatter missing required 'id' field")

    measured_at = data.get("measuredAt")
    if not measured_at:
        raise SessionParseError("frontmatter missing required 'measuredAt' field")

    return ParsedReadiness(
        id=event_id,
        measured_at=measured_at,
        readiness=data.get("readiness"),
        ln_rmssd=_num(data, "lnRmssd"),
        resting_hr=_int(data, "restingHr"),
        vo2max=_num(data, "vo2max"),
        recommendation=data.get("recommendation") or "",
        # Frontmatter fallback for reparse.py, which has no envelope to read
        # from. The live POST /v1/readiness path overrides these with the
        # envelope's values (see ingest_readiness) — the envelope is the
        # primary source, per SYNC.md "Daily step average". None (not 0)
        # means "no data available" — see ParsedReadiness's docstring.
        steps_avg_per_day=_optional_num(data, "stepsAvgPerDay"),
        steps_days_spanned=_optional_int(data, "stepsDaysSpanned"),
    )


def parse_scale_weighin_file(raw_bytes: bytes) -> ParsedScaleWeighin:
    """See docs/sync-ingestion/SCALE_SPEC.md. `id` is an ISO date string
    (at most one weigh-in per day), not the 8-hex scheme used elsewhere —
    still handled by the same string-coercing loader, harmlessly (it's
    already quoted on the wire, so this is just consistent, not required)."""
    text = raw_bytes.decode("utf-8")
    frontmatter, _body = _split_frontmatter(text)

    try:
        data = yaml.load(frontmatter, Loader=_StringIdLoader) or {}
    except yaml.YAMLError as exc:
        raise SessionParseError(f"invalid YAML frontmatter: {exc}") from exc

    if not isinstance(data, dict):
        raise SessionParseError("frontmatter did not parse to a mapping")

    weighin_id = data.get("id")
    if not weighin_id or not isinstance(weighin_id, str):
        raise SessionParseError("frontmatter missing required 'id' field")

    return ParsedScaleWeighin(
        id=weighin_id,
        date=data.get("date") or weighin_id,
        recorded_at=data.get("recordedAt"),
        weight_kg=_num(data, "weightKg"),
        bmi=_num(data, "bmi"),
        body_fat_percent=_num(data, "bodyFatPercent"),
        lean_mass_percent=_num(data, "leanMassPercent"),
    )


def parse_exercise_file(raw_bytes: bytes) -> ParsedExercise:
    """A repo `exercises/{slug}-{id}.md` file (docs/backup-server-brief.md
    Task 4). YAML shape: app repo docs/STORAGE.md §Exercise. Same tolerance
    as the session parser — unknown keys ignored, only `id` is required.

    `_`-prefixed basenames (the phone's debug test file) are handled one
    layer up (app.repo / reparse.py early-return before calling this), not
    here — this function has no access to the path."""
    data = _load_repo_frontmatter(raw_bytes)

    exercise_id = data.get("id")
    if not exercise_id or not isinstance(exercise_id, str):
        raise SessionParseError("exercise frontmatter missing required 'id' field")

    _, body = _split_frontmatter(raw_bytes.decode("utf-8"))

    return ParsedExercise(
        id=exercise_id,
        name=data.get("name"),
        type=data.get("type"),
        bodypart=data.get("bodypart"),
        link=data.get("link"),
        default_rep_min=_optional_int(data, "defaultRepRangeMin"),
        default_rep_max=_optional_int(data, "defaultRepRangeMax"),
        is_bodyweight=bool(data.get("isBodyweight", False)),
        bw_load_percent=_optional_int(data, "bwLoadPercent"),
        notes=body.strip(),
    )


def parse_routine_file(raw_bytes: bytes) -> ParsedRoutine:
    """A repo `routines/{slug}-{id}.md` file (docs/backup-server-brief.md
    Task 4). YAML shape: app repo docs/STORAGE.md §Routine — a flat header
    plus an `exercises:` list that becomes `routine_exercises` child rows.
    Same tolerance as above; only `id` is required."""
    data = _load_repo_frontmatter(raw_bytes)

    routine_id = data.get("id")
    if not routine_id or not isinstance(routine_id, str):
        raise SessionParseError("routine frontmatter missing required 'id' field")

    exercises = [
        RoutineExercise(
            position=position,
            exercise_id=str(raw.get("exerciseId") or ""),
            sets=_optional_int(raw, "sets"),
            rep_min=_optional_int(raw, "repRangeMin"),
            rep_max=_optional_int(raw, "repRangeMax"),
            time_per_set_seconds=_optional_int(raw, "timePerSetSeconds"),
            superset_with_next=bool(raw.get("supersetWithNext", False)),
            is_warmup=bool(raw.get("isWarmup", False)),
        )
        for position, raw in enumerate(data.get("exercises") or [])
    ]

    return ParsedRoutine(
        id=routine_id,
        name=data.get("name"),
        day=data.get("day"),
        enabled=bool(data.get("enabled", False)),
        exercises=exercises,
    )


def _load_repo_frontmatter(raw_bytes: bytes) -> dict:
    """Shared front-half of parse_exercise_file/parse_routine_file: decode,
    split the `---` frontmatter, parse it with the same string-id-preserving
    loader the other parsers use, and assert it's a mapping."""
    text = raw_bytes.decode("utf-8")
    frontmatter, _body = _split_frontmatter(text)
    try:
        data = yaml.load(frontmatter, Loader=_StringIdLoader) or {}
    except yaml.YAMLError as exc:
        raise SessionParseError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise SessionParseError("frontmatter did not parse to a mapping")
    return data


_ECG_MAGIC = b"MYGMECG1"
# magic(8) + sample_rate(4, i32be) + start_timestamp_ns(8, i64be)
_ECG_HEADER_SIZE = 20


def parse_ecg_file(raw_bytes: bytes) -> ParsedEcg:
    """See docs/sync-ingestion/ECG_SPEC.md "Binary format". `raw_bytes` here
    is already the *decompressed* .ecg bytes — gunzipping happens one layer
    up in app.ingestion, before this function ever sees the data, since the
    hash check (over the compressed bytes) must happen before decompression
    and this function's only job is the binary header/sample-stream shape.

    session_id (envelope-supplied) is not embedded in the binary format
    itself, so unlike the other three parsers this one can't recover an id
    from the bytes alone — callers attach envelope.record_id to the result.
    """
    if len(raw_bytes) < _ECG_HEADER_SIZE:
        raise SessionParseError(
            f"ECG file too short: {len(raw_bytes)} bytes, need at least {_ECG_HEADER_SIZE}"
        )

    magic = raw_bytes[:8]
    if magic != _ECG_MAGIC:
        raise SessionParseError(f"bad ECG magic: {magic!r}, expected {_ECG_MAGIC!r}")

    sample_rate = struct.unpack(">i", raw_bytes[8:12])[0]
    start_timestamp_ns = struct.unpack(">q", raw_bytes[12:20])[0]

    sample_bytes = raw_bytes[_ECG_HEADER_SIZE:]
    sample_count = len(sample_bytes) // 2  # Int16 BIG-ENDIAN, one per sample

    if sample_rate <= 0:
        raise SessionParseError(f"non-positive ECG sample rate: {sample_rate}")

    duration_seconds = sample_count / sample_rate

    return ParsedEcg(
        id="",  # filled in by the caller (envelope.record_id) — see docstring
        sample_rate=sample_rate,
        start_timestamp_ns=start_timestamp_ns,
        duration_seconds=duration_seconds,
    )


def read_ecg_samples(raw_bytes: bytes) -> list[int]:
    """Decodes the Int16 BIG-ENDIAN sample stream (µV) from an already-
    validated .ecg file — used by app.ecg_analysis, which needs the actual
    waveform rather than just the header parse_ecg_file returns. Deliberately
    not folded into parse_ecg_file/ParsedEcg: the ingestion path (app.
    ingestion) never needs the full sample list, only the header, and keeping
    them separate avoids holding a decoded sample array in memory on every
    ingest request when only analysis actually needs it. Any trailing odd
    byte (a half-written last sample) is silently dropped, same as
    parse_ecg_file's sample_count floor division."""
    if len(raw_bytes) < _ECG_HEADER_SIZE:
        raise SessionParseError(
            f"ECG file too short: {len(raw_bytes)} bytes, need at least {_ECG_HEADER_SIZE}"
        )
    sample_bytes = raw_bytes[_ECG_HEADER_SIZE:]
    sample_count = len(sample_bytes) // 2
    usable = sample_bytes[: sample_count * 2]
    return list(struct.unpack(f">{sample_count}h", usable))


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Splits on the `---` delimiters. Body may be empty."""
    if not text.startswith("---"):
        raise SessionParseError("file does not start with '---' frontmatter delimiter")

    # First line is the opening '---'; find the closing '---' on its own line.
    lines = text.split("\n")
    if lines[0].strip() != "---":
        raise SessionParseError("file does not start with '---' frontmatter delimiter")

    try:
        closing_index = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise SessionParseError("no closing '---' frontmatter delimiter found") from exc

    frontmatter = "\n".join(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1 :])
    return frontmatter, body


def _parse_exercises(raw_exercises: list) -> list[ExerciseSet]:
    exercise_sets: list[ExerciseSet] = []
    for exercise in raw_exercises:
        exercise_id = exercise.get("exerciseId")
        exercise_name = exercise.get("exerciseName")
        exercise_type = exercise.get("type")
        bodypart = exercise.get("bodypart")
        # Omitted when false — see Field notes.
        exclude_from_tonnage = bool(exercise.get("excludeFromTonnage", False))
        is_daily = bool(exercise.get("isDaily", False))

        is_cardio = (exercise_type or "").upper() == "CARDIO"
        is_stretch = (exercise_type or "").upper() == "STRETCH"

        for set_index, raw_set in enumerate(exercise.get("sets") or []):
            if is_cardio:
                exercise_sets.append(
                    ExerciseSet(
                        exercise_id=exercise_id,
                        exercise_name=exercise_name,
                        exercise_type=exercise_type,
                        bodypart=bodypart,
                        set_index=set_index,
                        reps=None,
                        weight=None,
                        exclude_from_tonnage=exclude_from_tonnage,
                        is_daily=is_daily,
                        is_bodyweight=False,
                        started_at=raw_set.get("startedAt") or None,
                        ended_at=raw_set.get("endedAt") or None,
                        avg_hr=_optional_int(raw_set, "avgHr"),
                        max_hr=_optional_int(raw_set, "maxHr"),
                    )
                )
                continue

            if is_stretch:
                exercise_sets.append(
                    ExerciseSet(
                        exercise_id=exercise_id,
                        exercise_name=exercise_name,
                        exercise_type=exercise_type,
                        bodypart=bodypart,
                        set_index=set_index,
                        reps=None,
                        weight=None,
                        exclude_from_tonnage=exclude_from_tonnage,
                        is_daily=is_daily,
                        is_bodyweight=False,
                        time_seconds=_optional_int(raw_set, "timeSeconds"),
                    )
                )
                continue

            exercise_sets.append(
                ExerciseSet(
                    exercise_id=exercise_id,
                    exercise_name=exercise_name,
                    exercise_type=exercise_type,
                    bodypart=bodypart,
                    set_index=set_index,
                    reps=int(raw_set.get("reps") or 0),
                    weight=round(float(raw_set.get("weight") or 0.0), 2),
                    exclude_from_tonnage=exclude_from_tonnage,
                    is_daily=is_daily,
                    # Distinguishes a completed bodyweight set from an
                    # untouched/empty one, which both have weight=0 without it
                    # (see ANALYSIS_SPEC.md's common_tonnage gap). Absent ->
                    # False, same "omitted means false" convention as the
                    # other per-set/per-exercise booleans.
                    is_bodyweight=bool(raw_set.get("isBodyweight", False)),
                    # Audit only — see the module docstring. Present only on
                    # bodyweight sets; None otherwise. Nothing computes from
                    # these (the phone already materialized `weight`).
                    bw_load_percent=_optional_int(raw_set, "bwLoadPercent"),
                    bw_base_weight_kg=_optional_num(raw_set, "bwBaseWeightKg"),
                )
            )
    return exercise_sets


def _num(data: dict, key: str) -> float:
    """Missing numeric field -> 0.0, per the sender's space-saving convention.
    Rounded to 2 decimals: the phone occasionally sends long floating-point
    tails (e.g. a computed TRIMP or drift figure) that carry no real
    precision — 2dp is all any consumer (the SQLite layer, the dashboard,
    the analysis JSON export) needs, so normalize here at the single point
    every scalar float field flows through."""
    return round(float(data.get(key) or 0), 2)


def _int(data: dict, key: str) -> int:
    return int(data.get(key) or 0)


def _optional_int(data: dict, key: str) -> int | None:
    """Like _int, but missing/None stays None rather than collapsing to 0 —
    for fields where 0 is a meaningfully different value from "not sent"
    (e.g. sessionRpe: an optional 1-10 rating, not a space-saving-omitted
    counter)."""
    value = data.get(key)
    return None if value is None else int(value)


def _optional_num(data: dict, key: str) -> float | None:
    """Float counterpart to _optional_int — see its docstring. Rounded to
    2 decimals for the same reason as _num."""
    value = data.get(key)
    return None if value is None else round(float(value), 2)
