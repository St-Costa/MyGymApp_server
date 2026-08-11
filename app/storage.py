"""SQLite schema, connection helper, and upsert logic.

This is the queryable, *re-derivable* layer described in
docs/sync-ingestion/SPEC.md: everything written here can be reconstructed
from the raw files under DATA_ROOT/raw/ via reparse.py. Never treat a row
here as more authoritative than the raw file it was parsed from.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id                       TEXT PRIMARY KEY,
  date                     TEXT,
  routine_id               TEXT,
  routine_name             TEXT,
  started_at               TEXT,
  completed_at             TEXT,
  total_tonnage            REAL,
  session_calories         REAL,
  session_trimp            REAL,
  vo2max                   REAL,
  ecg_beats                INTEGER,
  ecg_duration_sec         INTEGER,
  ecg_session_rmssd        REAL,
  ecg_pac_count            INTEGER,
  ecg_pause_count          INTEGER,
  ecg_irregular_beats      INTEGER,
  afib_suspicion_episodes  INTEGER,
  sdnn                     REAL,
  pnn50                    REAL,
  poincare_sd1             REAL,
  poincare_sd2             REAL,
  poincare_ratio           REAL,
  cardiac_drift_bpm_min    REAL,
  resting_hr               INTEGER,
  hrr60s                   INTEGER,
  session_rpe              INTEGER,
  session_load             REAL,
  notes                    TEXT,
  content_hash             TEXT NOT NULL,
  raw_path                 TEXT NOT NULL,
  received_at              TEXT NOT NULL,
  schema_version           INTEGER NOT NULL
);
-- Every app.analysis query and dashboard page load filters/orders sessions
-- by date range (WHERE date >= ? AND date <= ? ORDER BY date) — without this,
-- each of those is a full table scan that gets slower as history accumulates.
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);

CREATE TABLE IF NOT EXISTS exercise_sets (
  session_id            TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  exercise_id           TEXT NOT NULL,
  exercise_name         TEXT,
  exercise_type         TEXT,
  bodypart              TEXT,
  set_index             INTEGER NOT NULL,
  reps                  INTEGER,
  weight                REAL,
  exclude_from_tonnage  INTEGER NOT NULL DEFAULT 0,
  is_daily              INTEGER NOT NULL DEFAULT 0,
  is_bodyweight         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_exercise_sets_session ON exercise_sets(session_id);
-- app.analysis filters on exercise_id in PR lookups (all_time_tonnage_pr,
-- recent_heaviest_set_pr), exercise_history, and list_exercises' GROUP BY —
-- without this, each of those does a full table scan of exercise_sets.
CREATE INDEX IF NOT EXISTS idx_exercise_sets_exercise ON exercise_sets(exercise_id);

CREATE TABLE IF NOT EXISTS tonnage_by_bodypart (
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  bodypart    TEXT NOT NULL,
  tonnage     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tonnage_by_bodypart_session ON tonnage_by_bodypart(session_id);

-- Readiness events (docs/sync-ingestion/READINESS_SPEC.md). Independent of
-- sessions — an 8-hex id, but in its own namespace/table, since a readiness
-- event and a session could coincidentally share the same id string.
CREATE TABLE IF NOT EXISTS readiness_events (
  id              TEXT PRIMARY KEY,
  measured_at     TEXT NOT NULL,
  readiness       TEXT,
  ln_rmssd        REAL,
  resting_hr      INTEGER,
  vo2max          REAL,
  recommendation  TEXT,
  content_hash    TEXT NOT NULL,
  raw_path        TEXT NOT NULL,
  received_at     TEXT NOT NULL,
  schema_version  INTEGER NOT NULL
);
-- latest_readiness/readiness_daily_trend order/filter by measured_at.
CREATE INDEX IF NOT EXISTS idx_readiness_events_measured_at ON readiness_events(measured_at);

-- Scale weigh-ins (docs/sync-ingestion/SCALE_SPEC.md). `id` is an ISO date
-- string (at most one weigh-in per day), not the 8-hex scheme used
-- elsewhere — still its own namespace/table, no cross-type assumptions.
CREATE TABLE IF NOT EXISTS scale_weighins (
  id                 TEXT PRIMARY KEY,
  date               TEXT NOT NULL,
  recorded_at        TEXT,
  weight_kg          REAL,
  bmi                REAL,
  body_fat_percent   REAL,
  lean_mass_percent  REAL,
  content_hash       TEXT NOT NULL,
  raw_path           TEXT NOT NULL,
  received_at        TEXT NOT NULL,
  schema_version     INTEGER NOT NULL
);
-- scale_weekly_trend filters/orders by date over an 8-week window.
CREATE INDEX IF NOT EXISTS idx_scale_weighins_date ON scale_weighins(date);

-- Raw ECG recordings (docs/sync-ingestion/ECG_SPEC.md). Unlike the other
-- three record types, the server's copy is the *only* copy once the phone's
-- local file is gone (see ECG_SPEC.md "Retention asymmetry") — this table is
-- a header-only index over raw/ecg/{session_id}.ecg, not a place to lose
-- data if analysis logic changes; analysis results belong in their own
-- table(s), designed alongside that later work.
-- No FK to sessions(id): ECG_SPEC.md doesn't guarantee a session syncs
-- before its ECG recording (they're uploaded independently) — with
-- PRAGMA foreign_keys = ON (see connect() below), a hard FK here would
-- reject a legitimate ECG upload whenever it races ahead of its session,
-- which would silently lose the one copy of that waveform (see "Retention
-- asymmetry" in the spec). session_id is still the natural join key to
-- sessions.id in practice, just not DB-enforced.
CREATE TABLE IF NOT EXISTS ecg_recordings (
  session_id          TEXT PRIMARY KEY,
  raw_path            TEXT NOT NULL,
  -- Defaults to 0 (not a real sample rate) rather than NOT NULL-with-no-
  -- default, so the shared record_parse_failure() bare-minimum row (id/
  -- content_hash/raw_path/received_at/schema_version only, same as the other
  -- three record types) can still be written when header parsing fails —
  -- e.g. bad magic bytes. The raw file is still safely on disk either way.
  sample_rate         INTEGER NOT NULL DEFAULT 0,
  start_timestamp_ns  INTEGER,
  duration_seconds    REAL,
  content_hash        TEXT NOT NULL,
  received_at         TEXT NOT NULL,
  analyzed_at         TEXT,
  schema_version      INTEGER NOT NULL
);

-- Results of the server-side ECG analysis (docs/sync-ingestion/
-- ECG_ANALYSIS_HANDOFF.md): R-peak/HRV/arrhythmia metrics derived from the
-- raw waveform in ecg_recordings, a straight port of what the phone's
-- EcgAnalyzer.kt used to compute before local analysis was removed. Its own
-- table (not sessions' pre-existing ecg_* columns) so it can be dropped and
-- fully re-derived from ecg_recordings' raw files at any time via
-- reparse-style re-analysis, without touching the sessions row at all — the
-- same re-derivable-index principle as every other table here. No FK to
-- ecg_recordings for the same reason ecg_recordings has none to sessions:
-- analysis can be (re)run independently of when/whether the recording row
-- itself gets rewritten.
CREATE TABLE IF NOT EXISTS ecg_analysis (
  session_id               TEXT PRIMARY KEY,
  avg_hr                   REAL,
  session_rmssd            REAL,
  sdnn                     REAL,
  pnn50                    REAL,
  poincare_sd1             REAL,
  poincare_sd2             REAL,
  poincare_ratio           REAL,
  pac_count                INTEGER,
  pause_count              INTEGER,
  irregular_beats          INTEGER,
  afib_suspicion_episodes  INTEGER,
  peak_count               INTEGER NOT NULL,   -- detected R-peaks; diagnostic, not one of the 12
  -- Tier-1 advanced metrics (docs/sync-ingestion/ECG_ADVANCED_ANALYSIS.md).
  -- Same "None means no result" convention as the fields above.
  kurtosis_sqi             REAL,   -- signal quality: amplitude-distribution kurtosis
  b_sqi                    REAL,   -- signal quality: agreement with an independent 2nd detector
  dfa_alpha1               REAL,   -- fractal RR scaling exponent; ~0.75 ~= aerobic threshold (VT1)
  hrv_lf                   REAL,   -- frequency-domain HRV: low-frequency power
  hrv_hf                   REAL,   -- frequency-domain HRV: high-frequency (vagal) power
  hrv_lf_hf                REAL,   -- LF/HF ratio, coarse sympathovagal balance proxy
  sample_entropy           REAL,   -- nonlinear HRV: SampEn
  csi                       REAL,  -- nonlinear HRV: Cardiac Sympathetic Index
  cvi                       REAL,  -- nonlinear HRV: Cardiac Vagal Index
  hrr_1min                 REAL,   -- heart rate recovery, 1min after the recording's peak HR
  hrr_2min                 REAL,   -- heart rate recovery, 2min after the recording's peak HR
  -- Minutes in each heart-rate training zone (%HRR/Karvonen when a resting
  -- HR was available, %HRmax fallback otherwise — see hr_zones_used_hrr
  -- and app.ecg_analysis.compute_hr_zone_minutes). below_zone1_min is time
  -- under even Z1's floor (rest between sets), not itself a zone.
  hr_zone1_min             REAL,
  hr_zone2_min             REAL,
  hr_zone3_min             REAL,
  hr_zone4_min             REAL,
  hr_zone5_min             REAL,
  hr_below_zone1_min       REAL,
  hr_zones_used_hrr        INTEGER,  -- 0/1/NULL: which method computed the zones above
  analyzed_at              TEXT NOT NULL,
  schema_version           INTEGER NOT NULL
);

-- Records whose raw file was stored but whose parse step failed (step 6
-- "on failure" case in the spec). schema_version = 0 on the corresponding
-- row already marks this, but keeping the exception text separately makes
-- debugging a batch of failures much faster than grepping log files.
-- record_type + record_id together are the key, since ids are only unique
-- within their own record type (sessions/readiness_events/scale_weighins),
-- not globally.
CREATE TABLE IF NOT EXISTS parse_failures (
  record_type  TEXT NOT NULL,
  record_id    TEXT NOT NULL,
  raw_path     TEXT NOT NULL,
  error        TEXT NOT NULL,
  failed_at    TEXT NOT NULL,
  PRIMARY KEY (record_type, record_id)
);
"""

# Bumped by hand whenever the set of fields the parser extracts changes.
# Stamped into sessions.schema_version on every successful parse; reparse.py
# --stale finds rows below this value and re-parses them. Bumped to 3 for the
# addition of parse_ecg_file/ecg_recordings (docs/sync-ingestion/ECG_SPEC.md).
PARSER_SCHEMA_VERSION = 3

# schema_version stamped on a row written from step 6's "on failure" path —
# the raw file is safely stored but nothing has been successfully parsed yet.
UNPARSED_SCHEMA_VERSION = 0


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        _migrate_parse_failures_table(conn)
        conn.executescript(SCHEMA)
        _migrate_exercise_sets_is_bodyweight_column(conn)
        _migrate_sessions_rpe_columns(conn)
        _migrate_ecg_analysis_advanced_columns(conn)
        _migrate_ecg_analysis_hr_zone_columns(conn)


def _migrate_sessions_rpe_columns(conn: sqlite3.Connection) -> None:
    """One-time migration: `sessions.session_rpe`/`session_load` (session-RPE,
    ANALYSIS_SPEC.md §13) were added after `sessions` already existed in
    deployed databases — same reason as _migrate_exercise_sets_is_bodyweight_
    column above. Both stay NULL (not 0) for existing rows, same as any
    session synced before the phone started sending this field — session_rpe
    is an optional 1-10 rating, so unlike the other numeric session fields,
    "never answered" must stay distinguishable from an actual value."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "session_rpe" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN session_rpe INTEGER")
    if "session_load" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN session_load REAL")


def _migrate_exercise_sets_is_bodyweight_column(conn: sqlite3.Connection) -> None:
    """One-time migration: `exercise_sets.is_bodyweight` was added after
    `exercise_sets` already existed in deployed databases. `CREATE TABLE IF
    NOT EXISTS` in SCHEMA won't add a column to an already-existing table,
    so add it here if missing. Defaults to 0 (not bodyweight) for existing
    rows — correct for anything with weight>0, and the closest available
    guess for weight=0 rows recorded before this column existed (no data to
    distinguish "untouched set" from "bodyweight exercise" for those; a
    reparse won't fix this retroactively either, since the raw files
    themselves predate the phone sending isBodyweight at all)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(exercise_sets)")}
    if "is_bodyweight" not in columns:
        conn.execute(
            "ALTER TABLE exercise_sets ADD COLUMN is_bodyweight INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_ecg_analysis_advanced_columns(conn: sqlite3.Connection) -> None:
    """One-time migration: the tier-1 advanced metrics (docs/sync-ingestion/
    ECG_ADVANCED_ANALYSIS.md — SQI, DFA-alpha1, frequency-domain/nonlinear
    HRV, HRR) were added after `ecg_analysis` already existed in deployed
    databases. All nullable and left NULL for existing rows — same "no
    result yet" convention as every other field here; `analyze_ecg.py
    --stale` (ECG_ANALYSIS_SCHEMA_VERSION bumped alongside this) is what
    backfills them, not this migration."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ecg_analysis)")}
    new_columns = (
        "kurtosis_sqi",
        "b_sqi",
        "dfa_alpha1",
        "hrv_lf",
        "hrv_hf",
        "hrv_lf_hf",
        "sample_entropy",
        "csi",
        "cvi",
        "hrr_1min",
        "hrr_2min",
    )
    for column in new_columns:
        if column not in columns:
            conn.execute(f"ALTER TABLE ecg_analysis ADD COLUMN {column} REAL")  # noqa: S608


def _migrate_ecg_analysis_hr_zone_columns(conn: sqlite3.Connection) -> None:
    """One-time migration: HR-zone minutes (app.ecg_analysis.
    compute_hr_zone_minutes) were added after ecg_analysis already existed
    in deployed databases — same pattern/reasoning as
    _migrate_ecg_analysis_advanced_columns above."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ecg_analysis)")}
    new_columns = (
        "hr_zone1_min",
        "hr_zone2_min",
        "hr_zone3_min",
        "hr_zone4_min",
        "hr_zone5_min",
        "hr_below_zone1_min",
    )
    for column in new_columns:
        if column not in columns:
            conn.execute(f"ALTER TABLE ecg_analysis ADD COLUMN {column} REAL")  # noqa: S608
    # 0/1/NULL flag, added alongside the %HRR-vs-%HRmax switch — separate
    # ALTER since it's INTEGER, not REAL like the columns above.
    if "hr_zones_used_hrr" not in columns:
        conn.execute("ALTER TABLE ecg_analysis ADD COLUMN hr_zones_used_hrr INTEGER")


def _migrate_parse_failures_table(conn: sqlite3.Connection) -> None:
    """One-time migration: `parse_failures` originally had a `session_id`
    PK (back when sessions were the only record type). Adding readiness/
    scale weigh-ins required a `(record_type, record_id)` key instead, since
    ids are only unique within their own record type. `CREATE TABLE IF NOT
    EXISTS` in SCHEMA won't touch an already-existing table with the old
    shape, so detect and migrate it explicitly here before SCHEMA runs.
    Existing rows are session failures by construction (readiness/scale
    didn't exist yet when they could have been written), so they migrate
    with record_type='sessions' and record_id=<old session_id>.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'parse_failures'"
    ).fetchone()
    if row is None or "session_id" not in row[0]:
        return  # table doesn't exist yet, or already has the new shape

    conn.execute("ALTER TABLE parse_failures RENAME TO parse_failures_old")
    conn.execute(
        """
        CREATE TABLE parse_failures (
          record_type  TEXT NOT NULL,
          record_id    TEXT NOT NULL,
          raw_path     TEXT NOT NULL,
          error        TEXT NOT NULL,
          failed_at    TEXT NOT NULL,
          PRIMARY KEY (record_type, record_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO parse_failures (record_type, record_id, raw_path, error, failed_at)
        SELECT 'sessions', session_id, raw_path, error, failed_at FROM parse_failures_old
        """
    )
    conn.execute("DROP TABLE parse_failures_old")


@contextmanager
def connect(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@dataclass
class ExerciseSet:
    exercise_id: str
    exercise_name: str | None
    exercise_type: str | None
    bodypart: str | None
    set_index: int
    reps: int
    weight: float
    exclude_from_tonnage: bool
    is_daily: bool
    is_bodyweight: bool = False


@dataclass
class ParsedSession:
    """Everything step 6 needs to upsert, independent of HTTP/request concerns."""

    id: str
    date: str | None
    routine_id: str | None
    routine_name: str | None
    started_at: str | None
    completed_at: str
    total_tonnage: float
    session_calories: float
    session_trimp: float
    vo2max: float
    ecg_beats: int
    ecg_duration_sec: int
    ecg_session_rmssd: float
    ecg_pac_count: int
    ecg_pause_count: int
    ecg_irregular_beats: int
    afib_suspicion_episodes: int
    sdnn: float
    pnn50: float
    poincare_sd1: float
    poincare_sd2: float
    poincare_ratio: float
    cardiac_drift_bpm_min: float
    resting_hr: int
    hrr60s: int
    notes: str
    # Session-RPE (ANALYSIS_SPEC.md §13): optional 1-10 self-reported effort
    # rating and the derived Foster session-load (rpe * duration_minutes).
    # Unlike the fields above, "not answered" must stay None, not 0 — 0 isn't
    # a valid RPE value and collapsing "skipped" into 0 would be indistinguishable
    # from a (nonsensical) reported effort of zero.
    session_rpe: int | None = None
    session_load: float | None = None
    tonnage_by_bodypart: dict[str, float] = field(default_factory=dict)
    exercise_sets: list[ExerciseSet] = field(default_factory=list)


@dataclass
class ParsedReadiness:
    """See docs/sync-ingestion/READINESS_SPEC.md. `id` lives in its own
    namespace — never conflate with sessions.id even if they coincide."""

    id: str
    measured_at: str
    readiness: str | None
    ln_rmssd: float
    resting_hr: int
    vo2max: float
    recommendation: str


@dataclass
class ParsedScaleWeighin:
    """See docs/sync-ingestion/SCALE_SPEC.md. `id` is an ISO date string
    (at most one weigh-in per day), not the 8-hex scheme used elsewhere."""

    id: str
    date: str
    recorded_at: str | None
    weight_kg: float
    bmi: float
    body_fat_percent: float
    lean_mass_percent: float


@dataclass
class ParsedEcg:
    """See docs/sync-ingestion/ECG_SPEC.md. `id` is the sessionId (matches
    `sessions.id`), not a namespace of its own — one ECG recording per
    session, at most. `start_timestamp_ns` is stored verbatim; currently
    informational only, unused by any analysis (mirrors the phone's own
    reader, which doesn't consume it yet either)."""

    id: str
    sample_rate: int
    start_timestamp_ns: int | None
    duration_seconds: float


@dataclass
class EcgAnalysisResult:
    """One row of docs/sync-ingestion/ECG_ANALYSIS_HANDOFF.md's ported
    analysis: R-peak/HRV/arrhythmia metrics derived from a raw ECG waveform,
    plus the tier-1 advanced metrics from ECG_ADVANCED_ANALYSIS.md (SQI,
    DFA-alpha1, frequency-domain/nonlinear HRV, HRR). Every metric field is
    `None` when its underlying computation didn't run/couldn't produce a
    result ("no result", not zeros — see analyze_ecg_recording's docstring
    in app.ecg_analysis)."""

    session_id: str
    avg_hr: float | None
    session_rmssd: float | None
    sdnn: float | None
    pnn50: float | None
    poincare_sd1: float | None
    poincare_sd2: float | None
    poincare_ratio: float | None
    pac_count: int | None
    pause_count: int | None
    irregular_beats: int | None
    afib_suspicion_episodes: int | None
    peak_count: int
    # Tier-1 advanced metrics (docs/sync-ingestion/ECG_ADVANCED_ANALYSIS.md).
    kurtosis_sqi: float | None = None
    b_sqi: float | None = None
    dfa_alpha1: float | None = None
    hrv_lf: float | None = None
    hrv_hf: float | None = None
    hrv_lf_hf: float | None = None
    sample_entropy: float | None = None
    csi: float | None = None
    cvi: float | None = None
    hrr_1min: float | None = None
    hrr_2min: float | None = None
    # Minutes in each heart-rate training zone (app.ecg_analysis.
    # compute_hr_zone_minutes) — hr_below_zone1_min is time under even Z1's
    # floor (rest between sets), not itself a zone. hr_zones_used_hrr
    # records whether %HRR (Karvonen, a resting HR was available) or the
    # %HRmax fallback computed the zone values above — the two methods
    # aren't directly comparable, see compute_hr_zone_minutes' docstring.
    hr_zone1_min: float | None = None
    hr_zone2_min: float | None = None
    hr_zone3_min: float | None = None
    hr_zone4_min: float | None = None
    hr_zone5_min: float | None = None
    hr_below_zone1_min: float | None = None
    hr_zones_used_hrr: bool | None = None


# Tables that participate in the generic id -> content_hash / stale-lookup
# helpers below. Whitelisted rather than interpolating an arbitrary caller-
# supplied table name into SQL.
_RECORD_TABLES = {"sessions", "readiness_events", "scale_weighins", "ecg_recordings"}

# Most record tables' primary key column is named `id`; ecg_recordings' is
# `session_id` (per ECG_SPEC.md's suggested schema — it's the natural join
# key to sessions.id, though not FK-enforced, see the table's own comment).
# The generic helpers below look up the right column name here rather than
# assuming "id" everywhere.
_ID_COLUMNS = {"ecg_recordings": "session_id"}


def id_column(table: str) -> str:
    """Public for reparse.py, which needs to build its own lookup query
    against a record table's primary key column (usually `id`, but
    `ecg_recordings.session_id` — see _ID_COLUMNS above)."""
    return _ID_COLUMNS.get(table, "id")


# Internal alias, kept so existing call sites in this module read the same.
_id_column = id_column


def get_stored_content_hash(
    conn: sqlite3.Connection, record_id: str, table: str = "sessions"
) -> str | None:
    if table not in _RECORD_TABLES:
        raise ValueError(f"unknown table: {table}")
    id_column = _id_column(table)
    row = conn.execute(
        f"SELECT content_hash FROM {table} WHERE {id_column} = ?",
        (record_id,),  # noqa: S608
    ).fetchone()
    return row[0] if row else None


def upsert_parsed_session(
    conn: sqlite3.Connection,
    parsed: ParsedSession,
    *,
    content_hash: str,
    raw_path: str,
    received_at: str,
) -> None:
    """Step 6 "on success" path: write sessions + child rows.

    Child tables are delete-then-reinsert on every call so a re-sent,
    shorter version of a session never leaves stale extra rows behind —
    see the spec's "SQLite schema" section for why.
    """
    conn.execute(
        """
        INSERT INTO sessions (
            id, date, routine_id, routine_name, started_at, completed_at,
            total_tonnage, session_calories, session_trimp, vo2max,
            ecg_beats, ecg_duration_sec, ecg_session_rmssd, ecg_pac_count,
            ecg_pause_count, ecg_irregular_beats, afib_suspicion_episodes,
            sdnn, pnn50, poincare_sd1, poincare_sd2, poincare_ratio,
            cardiac_drift_bpm_min, resting_hr, hrr60s, session_rpe, session_load, notes,
            content_hash, raw_path, received_at, schema_version
        ) VALUES (
            :id, :date, :routine_id, :routine_name, :started_at, :completed_at,
            :total_tonnage, :session_calories, :session_trimp, :vo2max,
            :ecg_beats, :ecg_duration_sec, :ecg_session_rmssd, :ecg_pac_count,
            :ecg_pause_count, :ecg_irregular_beats, :afib_suspicion_episodes,
            :sdnn, :pnn50, :poincare_sd1, :poincare_sd2, :poincare_ratio,
            :cardiac_drift_bpm_min, :resting_hr, :hrr60s, :session_rpe, :session_load, :notes,
            :content_hash, :raw_path, :received_at, :schema_version
        )
        ON CONFLICT(id) DO UPDATE SET
            date=excluded.date, routine_id=excluded.routine_id,
            routine_name=excluded.routine_name, started_at=excluded.started_at,
            completed_at=excluded.completed_at, total_tonnage=excluded.total_tonnage,
            session_calories=excluded.session_calories, session_trimp=excluded.session_trimp,
            vo2max=excluded.vo2max, ecg_beats=excluded.ecg_beats,
            ecg_duration_sec=excluded.ecg_duration_sec,
            ecg_session_rmssd=excluded.ecg_session_rmssd,
            ecg_pac_count=excluded.ecg_pac_count, ecg_pause_count=excluded.ecg_pause_count,
            ecg_irregular_beats=excluded.ecg_irregular_beats,
            afib_suspicion_episodes=excluded.afib_suspicion_episodes,
            sdnn=excluded.sdnn, pnn50=excluded.pnn50, poincare_sd1=excluded.poincare_sd1,
            poincare_sd2=excluded.poincare_sd2, poincare_ratio=excluded.poincare_ratio,
            cardiac_drift_bpm_min=excluded.cardiac_drift_bpm_min, resting_hr=excluded.resting_hr,
            hrr60s=excluded.hrr60s, session_rpe=excluded.session_rpe,
            session_load=excluded.session_load, notes=excluded.notes,
            content_hash=excluded.content_hash,
            raw_path=excluded.raw_path, received_at=excluded.received_at,
            schema_version=excluded.schema_version
        """,
        {
            "id": parsed.id,
            "date": parsed.date,
            "routine_id": parsed.routine_id,
            "routine_name": parsed.routine_name,
            "started_at": parsed.started_at,
            "completed_at": parsed.completed_at,
            "total_tonnage": parsed.total_tonnage,
            "session_calories": parsed.session_calories,
            "session_trimp": parsed.session_trimp,
            "vo2max": parsed.vo2max,
            "ecg_beats": parsed.ecg_beats,
            "ecg_duration_sec": parsed.ecg_duration_sec,
            "ecg_session_rmssd": parsed.ecg_session_rmssd,
            "ecg_pac_count": parsed.ecg_pac_count,
            "ecg_pause_count": parsed.ecg_pause_count,
            "ecg_irregular_beats": parsed.ecg_irregular_beats,
            "afib_suspicion_episodes": parsed.afib_suspicion_episodes,
            "sdnn": parsed.sdnn,
            "pnn50": parsed.pnn50,
            "poincare_sd1": parsed.poincare_sd1,
            "poincare_sd2": parsed.poincare_sd2,
            "poincare_ratio": parsed.poincare_ratio,
            "cardiac_drift_bpm_min": parsed.cardiac_drift_bpm_min,
            "resting_hr": parsed.resting_hr,
            "hrr60s": parsed.hrr60s,
            "session_rpe": parsed.session_rpe,
            "session_load": parsed.session_load,
            "notes": parsed.notes,
            "content_hash": content_hash,
            "raw_path": raw_path,
            "received_at": received_at,
            "schema_version": PARSER_SCHEMA_VERSION,
        },
    )

    conn.execute("DELETE FROM exercise_sets WHERE session_id = ?", (parsed.id,))
    conn.execute("DELETE FROM tonnage_by_bodypart WHERE session_id = ?", (parsed.id,))

    conn.executemany(
        """
        INSERT INTO exercise_sets (
            session_id, exercise_id, exercise_name, exercise_type, bodypart,
            set_index, reps, weight, exclude_from_tonnage, is_daily, is_bodyweight
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                parsed.id,
                s.exercise_id,
                s.exercise_name,
                s.exercise_type,
                s.bodypart,
                s.set_index,
                s.reps,
                s.weight,
                int(s.exclude_from_tonnage),
                int(s.is_daily),
                int(s.is_bodyweight),
            )
            for s in parsed.exercise_sets
        ],
    )

    conn.executemany(
        "INSERT INTO tonnage_by_bodypart (session_id, bodypart, tonnage) VALUES (?, ?, ?)",
        [
            (parsed.id, bodypart, tonnage)
            for bodypart, tonnage in parsed.tonnage_by_bodypart.items()
        ],
    )

    # A successful parse supersedes any earlier failure record for this session.
    conn.execute(
        "DELETE FROM parse_failures WHERE record_type = 'sessions' AND record_id = ?",
        (parsed.id,),
    )


def upsert_parsed_readiness(
    conn: sqlite3.Connection,
    parsed: ParsedReadiness,
    *,
    content_hash: str,
    raw_path: str,
    received_at: str,
) -> None:
    """Step 6 "on success" path for POST /v1/readiness — no child tables, a
    readiness event is a single flat row."""
    conn.execute(
        """
        INSERT INTO readiness_events (
            id, measured_at, readiness, ln_rmssd, resting_hr, vo2max,
            recommendation, content_hash, raw_path, received_at, schema_version
        ) VALUES (
            :id, :measured_at, :readiness, :ln_rmssd, :resting_hr, :vo2max,
            :recommendation, :content_hash, :raw_path, :received_at, :schema_version
        )
        ON CONFLICT(id) DO UPDATE SET
            measured_at=excluded.measured_at, readiness=excluded.readiness,
            ln_rmssd=excluded.ln_rmssd, resting_hr=excluded.resting_hr,
            vo2max=excluded.vo2max, recommendation=excluded.recommendation,
            content_hash=excluded.content_hash, raw_path=excluded.raw_path,
            received_at=excluded.received_at, schema_version=excluded.schema_version
        """,
        {
            "id": parsed.id,
            "measured_at": parsed.measured_at,
            "readiness": parsed.readiness,
            "ln_rmssd": parsed.ln_rmssd,
            "resting_hr": parsed.resting_hr,
            "vo2max": parsed.vo2max,
            "recommendation": parsed.recommendation,
            "content_hash": content_hash,
            "raw_path": raw_path,
            "received_at": received_at,
            "schema_version": PARSER_SCHEMA_VERSION,
        },
    )
    conn.execute(
        "DELETE FROM parse_failures WHERE record_type = 'readiness_events' AND record_id = ?",
        (parsed.id,),
    )


def upsert_parsed_scale_weighin(
    conn: sqlite3.Connection,
    parsed: ParsedScaleWeighin,
    *,
    content_hash: str,
    raw_path: str,
    received_at: str,
) -> None:
    """Step 6 "on success" path for POST /v1/scale-weighins — no child
    tables, a weigh-in is a single flat row keyed by its ISO date id."""
    conn.execute(
        """
        INSERT INTO scale_weighins (
            id, date, recorded_at, weight_kg, bmi, body_fat_percent,
            lean_mass_percent, content_hash, raw_path, received_at, schema_version
        ) VALUES (
            :id, :date, :recorded_at, :weight_kg, :bmi, :body_fat_percent,
            :lean_mass_percent, :content_hash, :raw_path, :received_at, :schema_version
        )
        ON CONFLICT(id) DO UPDATE SET
            date=excluded.date, recorded_at=excluded.recorded_at,
            weight_kg=excluded.weight_kg, bmi=excluded.bmi,
            body_fat_percent=excluded.body_fat_percent,
            lean_mass_percent=excluded.lean_mass_percent,
            content_hash=excluded.content_hash, raw_path=excluded.raw_path,
            received_at=excluded.received_at, schema_version=excluded.schema_version
        """,
        {
            "id": parsed.id,
            "date": parsed.date,
            "recorded_at": parsed.recorded_at,
            "weight_kg": parsed.weight_kg,
            "bmi": parsed.bmi,
            "body_fat_percent": parsed.body_fat_percent,
            "lean_mass_percent": parsed.lean_mass_percent,
            "content_hash": content_hash,
            "raw_path": raw_path,
            "received_at": received_at,
            "schema_version": PARSER_SCHEMA_VERSION,
        },
    )
    conn.execute(
        "DELETE FROM parse_failures WHERE record_type = 'scale_weighins' AND record_id = ?",
        (parsed.id,),
    )


def upsert_parsed_ecg(
    conn: sqlite3.Connection,
    parsed: ParsedEcg,
    *,
    content_hash: str,
    raw_path: str,
    received_at: str,
) -> None:
    """Step 9 "on success" path for POST /v1/ecg — no child tables, header
    fields only. `analyzed_at` is left NULL/untouched on every upsert (even a
    re-parse of an existing row): it's the work queue for a later, separate
    analysis job, not something the header parser has any business setting."""
    conn.execute(
        """
        INSERT INTO ecg_recordings (
            session_id, raw_path, sample_rate, start_timestamp_ns, duration_seconds,
            content_hash, received_at, schema_version
        ) VALUES (
            :id, :raw_path, :sample_rate, :start_timestamp_ns, :duration_seconds,
            :content_hash, :received_at, :schema_version
        )
        ON CONFLICT(session_id) DO UPDATE SET
            raw_path=excluded.raw_path, sample_rate=excluded.sample_rate,
            start_timestamp_ns=excluded.start_timestamp_ns,
            duration_seconds=excluded.duration_seconds,
            content_hash=excluded.content_hash, received_at=excluded.received_at,
            schema_version=excluded.schema_version
        """,
        {
            "id": parsed.id,
            "raw_path": raw_path,
            "sample_rate": parsed.sample_rate,
            "start_timestamp_ns": parsed.start_timestamp_ns,
            "duration_seconds": parsed.duration_seconds,
            "content_hash": content_hash,
            "received_at": received_at,
            "schema_version": PARSER_SCHEMA_VERSION,
        },
    )
    conn.execute(
        "DELETE FROM parse_failures WHERE record_type = 'ecg_recordings' AND record_id = ?",
        (parsed.id,),
    )


# Bumped by hand whenever app.ecg_analysis's formulas change (a new detector,
# a different threshold, a bug fix in the R-peak/HRV/arrhythmia math) —
# independent of PARSER_SCHEMA_VERSION above, since re-analyzing a recording
# never requires re-parsing its header and vice versa. Existing rows with
# schema_version below this are stale and due for re-analysis.
# Bumped to 2: detect_r_peaks gained a gap-filling pass that recovers real
# beats missed when a sustained wide artifact inflates the trailing-max
# threshold — see app.ecg_analysis's module comment and the commit that
# introduced it. Rows analyzed under schema_version 1 can have
# significantly inflated RMSSD/SDNN/AFib-suspicion counts on recordings
# with this kind of artifact.
# Bumped to 3: added the tier-1 advanced metrics (SQI, DFA-alpha1,
# frequency-domain/nonlinear HRV, HRR) — see
# docs/sync-ingestion/ECG_ADVANCED_ANALYSIS.md. Rows below this version
# simply have NULL in all the new columns until re-analyzed.
# Bumped to 4: fixed two bugs found by running schema_version=3 against real
# gym-session recordings — HRR's peak-detection used unsmoothed instantaneous
# HR (a single noisy beat could dominate the "peak", producing 100+ bpm HRR
# values) and bSQI's phase-offset estimation could converge on the wrong
# systematic delay entirely on some recordings. Both are described in
# app.ecg_analysis's docstrings; rows analyzed under schema_version 3 can
# have unreliable hrr_1min/hrr_2min/b_sqi values.
# Bumped to 5: added HR-zone minutes (app.ecg_analysis.
# compute_hr_zone_minutes) — %HRmax training-zone time breakdown per
# recording. Rows below this version have NULL in the hr_zone*/
# hr_below_zone1_min columns until re-analyzed.
# Bumped to 6: _estimated_max_hr changed from Fox et al. alone (220-age) to
# the mean of three published formulas (Fox/Tanaka/Gulati) — see
# app.ecg_analysis's docstring for why. Rows analyzed under schema_version 5
# have hr_zone*/hr_below_zone1_min computed against a slightly different
# (Fox-only) max-HR estimate.
# Bumped to 7: compute_hr_zone_minutes switched from %HRmax to %HRR (Karvonen:
# resting_hr + pct*(max_hr - resting_hr)) whenever a resting HR is available —
# a 7-day trailing average of readiness_events.resting_hr, since resting HR
# changes little day to day in a stable athlete. See hr_zones_used_hrr on each
# row for which formula was actually used (NULL/0 rows fell back to %HRmax
# because no resting-HR reading existed in the trailing week).
ECG_ANALYSIS_SCHEMA_VERSION = 7


def upsert_ecg_analysis(
    conn: sqlite3.Connection, result: EcgAnalysisResult, *, analyzed_at: str
) -> None:
    """Writes one ecg_analysis row and stamps ecg_recordings.analyzed_at for
    the same session_id, so a re-run of the analyzed_at IS NULL queue never
    reprocesses a session it just finished. Both writes happen in the same
    caller-managed transaction (see app.storage.connect) so a crash between
    them can't leave the queue and the result table disagreeing."""
    conn.execute(
        """
        INSERT INTO ecg_analysis (
            session_id, avg_hr, session_rmssd, sdnn, pnn50,
            poincare_sd1, poincare_sd2, poincare_ratio,
            pac_count, pause_count, irregular_beats, afib_suspicion_episodes,
            peak_count, kurtosis_sqi, b_sqi, dfa_alpha1,
            hrv_lf, hrv_hf, hrv_lf_hf, sample_entropy, csi, cvi,
            hrr_1min, hrr_2min,
            hr_zone1_min, hr_zone2_min, hr_zone3_min, hr_zone4_min, hr_zone5_min,
            hr_below_zone1_min, hr_zones_used_hrr, analyzed_at, schema_version
        ) VALUES (
            :session_id, :avg_hr, :session_rmssd, :sdnn, :pnn50,
            :poincare_sd1, :poincare_sd2, :poincare_ratio,
            :pac_count, :pause_count, :irregular_beats, :afib_suspicion_episodes,
            :peak_count, :kurtosis_sqi, :b_sqi, :dfa_alpha1,
            :hrv_lf, :hrv_hf, :hrv_lf_hf, :sample_entropy, :csi, :cvi,
            :hrr_1min, :hrr_2min,
            :hr_zone1_min, :hr_zone2_min, :hr_zone3_min, :hr_zone4_min, :hr_zone5_min,
            :hr_below_zone1_min, :hr_zones_used_hrr, :analyzed_at, :schema_version
        )
        ON CONFLICT(session_id) DO UPDATE SET
            avg_hr=excluded.avg_hr, session_rmssd=excluded.session_rmssd,
            sdnn=excluded.sdnn, pnn50=excluded.pnn50,
            poincare_sd1=excluded.poincare_sd1, poincare_sd2=excluded.poincare_sd2,
            poincare_ratio=excluded.poincare_ratio, pac_count=excluded.pac_count,
            pause_count=excluded.pause_count, irregular_beats=excluded.irregular_beats,
            afib_suspicion_episodes=excluded.afib_suspicion_episodes,
            peak_count=excluded.peak_count,
            kurtosis_sqi=excluded.kurtosis_sqi, b_sqi=excluded.b_sqi,
            dfa_alpha1=excluded.dfa_alpha1, hrv_lf=excluded.hrv_lf,
            hrv_hf=excluded.hrv_hf, hrv_lf_hf=excluded.hrv_lf_hf,
            sample_entropy=excluded.sample_entropy, csi=excluded.csi, cvi=excluded.cvi,
            hrr_1min=excluded.hrr_1min, hrr_2min=excluded.hrr_2min,
            hr_zone1_min=excluded.hr_zone1_min, hr_zone2_min=excluded.hr_zone2_min,
            hr_zone3_min=excluded.hr_zone3_min, hr_zone4_min=excluded.hr_zone4_min,
            hr_zone5_min=excluded.hr_zone5_min,
            hr_below_zone1_min=excluded.hr_below_zone1_min,
            hr_zones_used_hrr=excluded.hr_zones_used_hrr,
            analyzed_at=excluded.analyzed_at,
            schema_version=excluded.schema_version
        """,
        {
            "session_id": result.session_id,
            "avg_hr": result.avg_hr,
            "session_rmssd": result.session_rmssd,
            "sdnn": result.sdnn,
            "pnn50": result.pnn50,
            "poincare_sd1": result.poincare_sd1,
            "poincare_sd2": result.poincare_sd2,
            "poincare_ratio": result.poincare_ratio,
            "pac_count": result.pac_count,
            "pause_count": result.pause_count,
            "irregular_beats": result.irregular_beats,
            "afib_suspicion_episodes": result.afib_suspicion_episodes,
            "peak_count": result.peak_count,
            "kurtosis_sqi": result.kurtosis_sqi,
            "b_sqi": result.b_sqi,
            "dfa_alpha1": result.dfa_alpha1,
            "hrv_lf": result.hrv_lf,
            "hrv_hf": result.hrv_hf,
            "hrv_lf_hf": result.hrv_lf_hf,
            "sample_entropy": result.sample_entropy,
            "csi": result.csi,
            "cvi": result.cvi,
            "hrr_1min": result.hrr_1min,
            "hrr_2min": result.hrr_2min,
            "hr_zone1_min": result.hr_zone1_min,
            "hr_zone2_min": result.hr_zone2_min,
            "hr_zone3_min": result.hr_zone3_min,
            "hr_zone4_min": result.hr_zone4_min,
            "hr_zone5_min": result.hr_zone5_min,
            "hr_below_zone1_min": result.hr_below_zone1_min,
            "hr_zones_used_hrr": (
                None if result.hr_zones_used_hrr is None else int(result.hr_zones_used_hrr)
            ),
            "analyzed_at": analyzed_at,
            "schema_version": ECG_ANALYSIS_SCHEMA_VERSION,
        },
    )
    conn.execute(
        "UPDATE ecg_recordings SET analyzed_at = ? WHERE session_id = ?",
        (analyzed_at, result.session_id),
    )


def get_unanalyzed_ecg_recordings(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """The analyzed_at IS NULL work queue ECG_SPEC.md sets up: recordings
    whose raw file is stored but which have never been analyzed at all.
    Returns (session_id, raw_path) pairs, same shape as get_stale_records."""
    rows = conn.execute(
        "SELECT session_id, raw_path FROM ecg_recordings WHERE analyzed_at IS NULL"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def get_stale_ecg_analysis(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Recordings that *have* been analyzed, but by an older
    ECG_ANALYSIS_SCHEMA_VERSION — due for re-analysis after a formula/
    threshold change, same "stale" concept as get_stale_records but scoped
    to ecg_analysis.schema_version rather than ecg_recordings' own."""
    rows = conn.execute(
        """
        SELECT ecg_recordings.session_id, ecg_recordings.raw_path
        FROM ecg_recordings
        JOIN ecg_analysis ON ecg_analysis.session_id = ecg_recordings.session_id
        WHERE ecg_analysis.schema_version < ?
        """,
        (ECG_ANALYSIS_SCHEMA_VERSION,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def record_parse_failure(
    conn: sqlite3.Connection,
    *,
    table: str,
    record_type: str,
    record_id: str,
    content_hash: str,
    raw_path: str,
    received_at: str,
    error: str,
) -> None:
    """Step 6 "on failure" path, shared by all three record types.

    The raw file (step 5) already succeeded — that's what matters for not
    losing data. This just records enough (a bare-minimum row in the
    record's own table, plus the exception text in parse_failures) to find
    and retry the record later via `reparse.py`, without ever failing the
    HTTP request over it.
    """
    if table not in _RECORD_TABLES:
        raise ValueError(f"unknown table: {table}")
    id_column = _id_column(table)
    conn.execute(
        f"""
        INSERT INTO {table} ({id_column}, content_hash, raw_path, received_at, schema_version)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT({id_column}) DO UPDATE SET
            content_hash=excluded.content_hash, raw_path=excluded.raw_path,
            received_at=excluded.received_at, schema_version=excluded.schema_version
        """,  # noqa: S608 — table/id_column are whitelist-checked above
        (record_id, content_hash, raw_path, received_at, UNPARSED_SCHEMA_VERSION),
    )
    conn.execute(
        """
        INSERT INTO parse_failures (record_type, record_id, raw_path, error, failed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(record_type, record_id) DO UPDATE SET
            raw_path=excluded.raw_path, error=excluded.error, failed_at=excluded.failed_at
        """,
        (record_type, record_id, raw_path, error, received_at),
    )


def get_stale_records(conn: sqlite3.Connection, table: str = "sessions") -> list[tuple[str, str]]:
    """Records in `table` whose schema_version is behind the current parser version."""
    if table not in _RECORD_TABLES:
        raise ValueError(f"unknown table: {table}")
    id_column = _id_column(table)
    rows = conn.execute(
        f"SELECT {id_column}, raw_path FROM {table} WHERE schema_version < ?",  # noqa: S608
        (PARSER_SCHEMA_VERSION,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def get_all_records(conn: sqlite3.Connection, table: str = "sessions") -> list[tuple[str, str]]:
    if table not in _RECORD_TABLES:
        raise ValueError(f"unknown table: {table}")
    id_column = _id_column(table)
    rows = conn.execute(f"SELECT {id_column}, raw_path FROM {table}").fetchall()  # noqa: S608
    return [(r[0], r[1]) for r in rows]
