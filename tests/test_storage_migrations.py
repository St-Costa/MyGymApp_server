"""Regression tests for app.storage's one-time schema migrations —
init_db must upgrade an already-existing database with the pre-migration
shape, not just create the current schema fresh (CREATE TABLE IF NOT EXISTS
silently no-ops against an existing table, which is exactly how the
exercise_sets.is_bodyweight gap was caught against the real deployment)."""

import sqlite3

from app.storage import init_db


def test_init_db_adds_is_bodyweight_to_existing_exercise_sets_table(tmp_path) -> None:
    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE exercise_sets (
          session_id            TEXT NOT NULL,
          exercise_id           TEXT NOT NULL,
          exercise_name         TEXT,
          exercise_type         TEXT,
          bodypart               TEXT,
          set_index             INTEGER NOT NULL,
          reps                  INTEGER,
          weight                REAL,
          exclude_from_tonnage  INTEGER NOT NULL DEFAULT 0,
          is_daily              INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """INSERT INTO exercise_sets
           (session_id, exercise_id, set_index, reps, weight, exclude_from_tonnage, is_daily)
           VALUES ('s1', 'ex-a', 0, 8, 80.0, 0, 0)"""
    )
    conn.commit()
    conn.close()

    init_db(db_path)

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(exercise_sets)")}
    assert "is_bodyweight" in columns

    row = conn.execute("SELECT is_bodyweight FROM exercise_sets WHERE session_id = 's1'").fetchone()
    assert row == (0,)  # existing rows default to not-bodyweight
    conn.close()


def test_init_db_adds_session_rpe_columns_to_existing_sessions_table(tmp_path) -> None:
    db_path = tmp_path / "old_sessions.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE sessions (
          id             TEXT PRIMARY KEY,
          date           TEXT,
          total_tonnage  REAL,
          content_hash   TEXT NOT NULL,
          raw_path       TEXT NOT NULL,
          received_at    TEXT NOT NULL,
          schema_version INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """INSERT INTO sessions (id, date, total_tonnage, content_hash, raw_path,
                                  received_at, schema_version)
           VALUES ('s1', '2026-01-01', 1000.0, 'sha256:x', 'x.md', '2026-01-01T00:00:00Z', 1)"""
    )
    conn.commit()
    conn.close()

    init_db(db_path)

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "session_rpe" in columns
    assert "session_load" in columns

    row = conn.execute("SELECT session_rpe, session_load FROM sessions WHERE id = 's1'").fetchone()
    assert row == (None, None)  # existing rows stay NULL, not 0 — see storage.py's migration note
    conn.close()


def test_init_db_adds_cardio_columns_to_existing_exercise_sets_table(tmp_path) -> None:
    db_path = tmp_path / "old_cardio.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE exercise_sets (
          session_id            TEXT NOT NULL,
          exercise_id           TEXT NOT NULL,
          exercise_name         TEXT,
          exercise_type         TEXT,
          bodypart               TEXT,
          set_index             INTEGER NOT NULL,
          reps                  INTEGER,
          weight                REAL,
          exclude_from_tonnage  INTEGER NOT NULL DEFAULT 0,
          is_daily              INTEGER NOT NULL DEFAULT 0,
          is_bodyweight         INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """INSERT INTO exercise_sets
           (session_id, exercise_id, set_index, reps, weight, exclude_from_tonnage,
            is_daily, is_bodyweight)
           VALUES ('s1', 'ex-a', 0, 8, 80.0, 0, 0, 0)"""
    )
    conn.commit()
    conn.close()

    init_db(db_path)

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(exercise_sets)")}
    assert {"started_at", "ended_at", "avg_hr", "max_hr"} <= columns

    row = conn.execute(
        "SELECT started_at, ended_at, avg_hr, max_hr FROM exercise_sets WHERE session_id = 's1'"
    ).fetchone()
    assert row == (None, None, None, None)  # pre-existing FORZA/STRETCH rows stay NULL
    conn.close()


def test_init_db_adds_bodyweight_load_columns_to_existing_exercise_sets_table(tmp_path) -> None:
    db_path = tmp_path / "old_bwload.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE exercise_sets (
          session_id            TEXT NOT NULL,
          exercise_id           TEXT NOT NULL,
          exercise_name         TEXT,
          exercise_type         TEXT,
          bodypart               TEXT,
          set_index             INTEGER NOT NULL,
          reps                  INTEGER,
          weight                REAL,
          exclude_from_tonnage  INTEGER NOT NULL DEFAULT 0,
          is_daily              INTEGER NOT NULL DEFAULT 0,
          is_bodyweight         INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """INSERT INTO exercise_sets
           (session_id, exercise_id, set_index, reps, weight, exclude_from_tonnage,
            is_daily, is_bodyweight)
           VALUES ('s1', 'ex-a', 0, 12, 0.0, 0, 0, 1)"""
    )
    conn.commit()
    conn.close()

    init_db(db_path)

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(exercise_sets)")}
    assert {"bw_load_percent", "bw_base_weight_kg"} <= columns

    row = conn.execute(
        "SELECT bw_load_percent, bw_base_weight_kg FROM exercise_sets WHERE session_id = 's1'"
    ).fetchone()
    assert row == (None, None)  # pre-existing bodyweight rows stay NULL — weight already stands
    conn.close()


def test_init_db_is_idempotent_on_already_migrated_table(tmp_path) -> None:
    db_path = tmp_path / "new.sqlite3"
    init_db(db_path)
    init_db(db_path)  # must not raise (e.g. "duplicate column") on a second call

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(exercise_sets)")}
    assert "is_bodyweight" in columns
    conn.close()
