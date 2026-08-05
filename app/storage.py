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
  notes                    TEXT,
  content_hash             TEXT NOT NULL,
  raw_path                 TEXT NOT NULL,
  received_at              TEXT NOT NULL,
  schema_version           INTEGER NOT NULL
);

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
  is_daily              INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_exercise_sets_session ON exercise_sets(session_id);

CREATE TABLE IF NOT EXISTS tonnage_by_bodypart (
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  bodypart    TEXT NOT NULL,
  tonnage     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tonnage_by_bodypart_session ON tonnage_by_bodypart(session_id);

-- Sessions whose raw file was stored but whose parse step failed (step 6
-- "on failure" case in the spec). schema_version = 0 in `sessions` already
-- marks this, but keeping the exception text separately makes debugging a
-- batch of failures much faster than grepping log files by session id.
CREATE TABLE IF NOT EXISTS parse_failures (
  session_id  TEXT PRIMARY KEY,
  raw_path    TEXT NOT NULL,
  error       TEXT NOT NULL,
  failed_at   TEXT NOT NULL
);
"""

# Bumped by hand whenever the set of fields the parser extracts changes.
# Stamped into sessions.schema_version on every successful parse; reparse.py
# --stale finds rows below this value and re-parses them.
PARSER_SCHEMA_VERSION = 1

# schema_version stamped on a row written from step 6's "on failure" path —
# the raw file is safely stored but nothing has been successfully parsed yet.
UNPARSED_SCHEMA_VERSION = 0


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


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
    tonnage_by_bodypart: dict[str, float] = field(default_factory=dict)
    exercise_sets: list[ExerciseSet] = field(default_factory=list)


def get_stored_content_hash(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute("SELECT content_hash FROM sessions WHERE id = ?", (session_id,)).fetchone()
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
            cardiac_drift_bpm_min, resting_hr, hrr60s, notes,
            content_hash, raw_path, received_at, schema_version
        ) VALUES (
            :id, :date, :routine_id, :routine_name, :started_at, :completed_at,
            :total_tonnage, :session_calories, :session_trimp, :vo2max,
            :ecg_beats, :ecg_duration_sec, :ecg_session_rmssd, :ecg_pac_count,
            :ecg_pause_count, :ecg_irregular_beats, :afib_suspicion_episodes,
            :sdnn, :pnn50, :poincare_sd1, :poincare_sd2, :poincare_ratio,
            :cardiac_drift_bpm_min, :resting_hr, :hrr60s, :notes,
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
            hrr60s=excluded.hrr60s, notes=excluded.notes, content_hash=excluded.content_hash,
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
            set_index, reps, weight, exclude_from_tonnage, is_daily
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    conn.execute("DELETE FROM parse_failures WHERE session_id = ?", (parsed.id,))


def record_unparsed_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    content_hash: str,
    raw_path: str,
    received_at: str,
    error: str,
) -> None:
    """Step 6 "on failure" path.

    The raw file (step 5) already succeeded — that's what matters for not
    losing data. This just records enough to find and retry the session
    later via `reparse.py`, without ever failing the HTTP request over it.
    """
    conn.execute(
        """
        INSERT INTO sessions (id, content_hash, raw_path, received_at, schema_version)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            content_hash=excluded.content_hash, raw_path=excluded.raw_path,
            received_at=excluded.received_at, schema_version=excluded.schema_version
        """,
        (session_id, content_hash, raw_path, received_at, UNPARSED_SCHEMA_VERSION),
    )
    conn.execute(
        """
        INSERT INTO parse_failures (session_id, raw_path, error, failed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            raw_path=excluded.raw_path, error=excluded.error, failed_at=excluded.failed_at
        """,
        (session_id, raw_path, error, received_at),
    )


def get_stale_sessions(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Sessions whose schema_version is behind the current parser version."""
    rows = conn.execute(
        "SELECT id, raw_path FROM sessions WHERE schema_version < ?",
        (PARSER_SCHEMA_VERSION,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def get_all_sessions(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT id, raw_path FROM sessions").fetchall()
    return [(r[0], r[1]) for r in rows]
