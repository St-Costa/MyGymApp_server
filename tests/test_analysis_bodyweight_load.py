"""latest_bodyweight_load — the audit trail behind a bodyweight exercise's
materialized `weight` (see app.parsing's isBodyweight note). Explanatory
only; no other analysis path consumes it."""

import sqlite3

import pytest

from app.analysis import latest_bodyweight_load
from app.storage import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


def _insert_session(conn, session_id, date):
    conn.execute(
        """INSERT INTO sessions (id, date, content_hash, raw_path, received_at, schema_version)
           VALUES (?, ?, 'sha256:x', ?, '2026-01-01T00:00:00Z', 7)""",
        (session_id, date, f"{date}.md"),
    )


def _insert_set(conn, session_id, exercise_id, set_index, **fields):
    columns = {
        "session_id": session_id,
        "exercise_id": exercise_id,
        "set_index": set_index,
        "reps": 10,
        "weight": 0.0,
        "exclude_from_tonnage": 0,
        "is_daily": 0,
        "is_bodyweight": 1,
        "bw_load_percent": None,
        "bw_base_weight_kg": None,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO exercise_sets ({cols}) VALUES ({placeholders})", list(columns.values())
    )
    conn.commit()


def test_returns_none_when_exercise_never_logged_as_bodyweight(conn):
    _insert_session(conn, "s1", "2026-08-01")
    _insert_set(conn, "s1", "ex-a", 0, weight=80.0, is_bodyweight=0)
    assert latest_bodyweight_load(conn, "ex-a") is None


def test_returns_none_when_bodyweight_set_has_no_audit_fields(conn):
    # A bodyweight set synced before the phone sent the audit pair — nothing
    # to explain, weight already stands on its own.
    _insert_session(conn, "s1", "2026-08-01")
    _insert_set(conn, "s1", "ex-a", 0, weight=61.5)
    assert latest_bodyweight_load(conn, "ex-a") is None


def test_returns_latest_sessions_audit_fields(conn):
    _insert_session(conn, "s1", "2026-07-01")
    _insert_set(conn, "s1", "ex-a", 0, weight=60.0, bw_load_percent=75, bw_base_weight_kg=80.0)
    _insert_session(conn, "s2", "2026-08-01")
    _insert_set(conn, "s2", "ex-a", 0, weight=61.5, bw_load_percent=75, bw_base_weight_kg=82.0)

    load = latest_bodyweight_load(conn, "ex-a")
    assert load is not None
    assert load.load_percent == 75
    assert load.base_weight_kg == 82.0
    assert load.weight == 61.5
    assert load.session_date == "2026-08-01"


def test_no_weigh_in_case_keeps_base_weight_none_and_weight_zero(conn):
    # No scale weigh-in at completion -> phone froze weight at 0 but still
    # recorded the load percent. base_weight_kg stays None.
    _insert_session(conn, "s1", "2026-08-01")
    _insert_set(conn, "s1", "ex-a", 0, weight=0.0, bw_load_percent=50, bw_base_weight_kg=None)

    load = latest_bodyweight_load(conn, "ex-a")
    assert load is not None
    assert load.load_percent == 50
    assert load.base_weight_kg is None
    assert load.weight == 0.0
