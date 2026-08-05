"""Parses the Markdown-with-YAML-frontmatter session format into a ParsedSession.

This is the *one* parsing function shared by the live POST /v1/sessions
handler (app/routes.py) and the standalone reparse.py script — per the spec,
these must never diverge. Everything here is pure (bytes in, ParsedSession
out, or an exception) and touches neither the network nor the database, so
it's trivially testable and safe to re-run offline against files already on
disk.

Field rules (see docs/sync-ingestion/SPEC.md "Field notes" for the source of
truth):
- Any numeric field can be entirely absent from the YAML when its value is 0
  — treat missing as 0/0.0, never as an error or None.
- `tonnageByBodypart` entries with value 0 are dropped entirely rather than
  written as 0.0 — so only keys actually present matter.
- `completedAt` may be `""` (abandoned session) — store as-is, not an error.
- `excludeFromTonnage` / `isDaily` are omitted when false — missing means False.
- Unrecognized top-level YAML keys are ignored, not an error — the phone app
  evolves independently and may add fields this parser doesn't know about yet.
"""

import yaml

from app.storage import ExerciseSet, ParsedSession


class SessionParseError(ValueError):
    """Raised when the file doesn't look like a valid session (bad frontmatter,
    missing required identity fields, etc). Callers (routes.py, reparse.py)
    catch this — per the raw-first principle, a parse failure must never be
    allowed to take down the raw write or crash the reparse batch."""


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
        str(bodypart): float(tonnage)
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
        notes=body.strip(),
        tonnage_by_bodypart=tonnage_by_bodypart,
        exercise_sets=exercise_sets,
    )


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

        for set_index, raw_set in enumerate(exercise.get("sets") or []):
            exercise_sets.append(
                ExerciseSet(
                    exercise_id=exercise_id,
                    exercise_name=exercise_name,
                    exercise_type=exercise_type,
                    bodypart=bodypart,
                    set_index=set_index,
                    reps=int(raw_set.get("reps") or 0),
                    weight=float(raw_set.get("weight") or 0.0),
                    exclude_from_tonnage=exclude_from_tonnage,
                    is_daily=is_daily,
                )
            )
    return exercise_sets


def _num(data: dict, key: str) -> float:
    """Missing numeric field -> 0.0, per the sender's space-saving convention."""
    return float(data.get(key) or 0)


def _int(data: dict, key: str) -> int:
    return int(data.get(key) or 0)
