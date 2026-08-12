"""Unit tests for app.ingestion's DB-aware lookup helpers
(resting_hr_for_hr_zones, weight_kg_for_calorie_recalc) — pure
sqlite3.Connection-in, value-out, no HTTP/FastAPI involved. See
test_ecg_endpoint.py/test_analyze_ecg.py for their end-to-end wiring."""

import sqlite3

import pytest

from app.ingestion import resting_hr_for_hr_zones, weight_kg_for_calorie_recalc
from app.storage import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


def _insert_session(conn, session_id, date, *, resting_hr=None):
    conn.execute(
        """INSERT INTO sessions (id, date, content_hash, raw_path, received_at,
                                  schema_version, resting_hr)
           VALUES (?, ?, 'sha256:x', ?, '2026-01-01T00:00:00Z', 1, ?)""",
        (session_id, date, f"{date}.md", resting_hr),
    )
    conn.commit()


def _insert_readiness(conn, event_id, measured_at, *, resting_hr):
    conn.execute(
        """INSERT INTO readiness_events (id, measured_at, content_hash, raw_path,
                                          received_at, schema_version, resting_hr)
           VALUES (?, ?, 'sha256:x', ?, '2026-01-01T00:00:00Z', 1, ?)""",
        (event_id, measured_at, f"{event_id}.md", resting_hr),
    )
    conn.commit()


def _insert_scale_weighin(conn, weighin_id, date, *, weight_kg):
    conn.execute(
        """INSERT INTO scale_weighins (id, date, content_hash, raw_path, received_at,
                                        schema_version, weight_kg)
           VALUES (?, ?, 'sha256:x', ?, '2026-01-01T00:00:00Z', 1, ?)""",
        (weighin_id, date, f"{weighin_id}.md", weight_kg),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# resting_hr_for_hr_zones
# ---------------------------------------------------------------------------


def test_prefers_readiness_events_7day_average_over_session_value(conn):
    """When both a readiness_events reading and the session's own resting_hr
    exist, the 7-day readiness average wins — it's the dedicated morning
    HRV measurement, a more controlled reading than whatever the phone
    logged during a workout."""
    _insert_session(conn, "s1", "2026-08-10", resting_hr=70)
    _insert_readiness(conn, "r1", "2026-08-09T07:00:00Z", resting_hr=58)

    result = resting_hr_for_hr_zones(conn, "s1", "2026-08-10T00:00:00Z")
    assert result == pytest.approx(58.0)


def test_falls_back_to_session_resting_hr_when_no_readiness_in_window(conn):
    """No readiness_events reading in the trailing 7 days -> fall back to
    this session's own sessions.resting_hr (the phone's per-session
    reading) rather than giving up on a resting HR entirely — see
    resting_hr_for_hr_zones' docstring for why this is a reasonable second
    choice."""
    _insert_session(conn, "s1", "2026-08-10", resting_hr=62)
    # A readiness reading exists, but far outside the 7-day trailing window.
    _insert_readiness(conn, "r1", "2026-01-01T07:00:00Z", resting_hr=58)

    result = resting_hr_for_hr_zones(conn, "s1", "2026-08-10T00:00:00Z")
    assert result == pytest.approx(62.0)


def test_none_when_neither_readiness_nor_session_resting_hr_available(conn):
    _insert_session(conn, "s1", "2026-08-10", resting_hr=None)

    assert resting_hr_for_hr_zones(conn, "s1", "2026-08-10T00:00:00Z") is None


def test_session_resting_hr_zero_is_treated_as_unmeasured(conn):
    """0 is the phone's "omitted means unmeasured" convention (same as
    everywhere else in this codebase) — must not be used as a real
    fallback value."""
    _insert_session(conn, "s1", "2026-08-10", resting_hr=0)

    assert resting_hr_for_hr_zones(conn, "s1", "2026-08-10T00:00:00Z") is None


def test_no_matching_session_falls_back_to_received_at_anchor(conn):
    """A debug-* synthetic id (or an ECG that arrived before its session
    synced) has no sessions row at all — the readiness lookup still uses
    received_at's own date as the anchor, and the session-resting_hr
    fallback is simply unavailable (there's no session row to read it
    from), not an error."""
    _insert_readiness(conn, "r1", "2026-08-09T07:00:00Z", resting_hr=58)

    result = resting_hr_for_hr_zones(conn, "debug-12345", "2026-08-10T00:00:00Z")
    assert result == pytest.approx(58.0)


def test_no_matching_session_and_no_readiness_returns_none(conn):
    assert resting_hr_for_hr_zones(conn, "debug-12345", "2026-08-10T00:00:00Z") is None


# ---------------------------------------------------------------------------
# weight_kg_for_calorie_recalc
# ---------------------------------------------------------------------------


def test_weight_uses_nearest_scale_weighin_to_session_date(conn):
    _insert_session(conn, "s1", "2026-08-10")
    _insert_scale_weighin(conn, "w1", "2026-08-09", weight_kg=80.5)

    result = weight_kg_for_calorie_recalc(conn, "s1", "2026-08-10T00:00:00Z")
    assert result == pytest.approx(80.5)


def test_weight_none_when_no_scale_weighin_available(conn):
    _insert_session(conn, "s1", "2026-08-10")

    assert weight_kg_for_calorie_recalc(conn, "s1", "2026-08-10T00:00:00Z") is None
