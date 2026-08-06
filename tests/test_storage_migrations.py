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


def test_init_db_is_idempotent_on_already_migrated_table(tmp_path) -> None:
    db_path = tmp_path / "new.sqlite3"
    init_db(db_path)
    init_db(db_path)  # must not raise (e.g. "duplicate column") on a second call

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(exercise_sets)")}
    assert "is_bodyweight" in columns
    conn.close()
