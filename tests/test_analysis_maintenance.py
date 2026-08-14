"""estimated_maintenance_calories — NOT in ANALYSIS_SPEC.md, added on user
request as a formula-only TDEE range estimate for the dashboard's
"Daily maintenance kcal" box. See app.analysis's module comment above the
function for why it deliberately ignores the weight *trend* (circular with
the diet decision it's meant to inform) and only uses the latest single
weight reading."""

import sqlite3

import pytest

from app.analysis import estimated_maintenance_calories
from app.config import ASSUMED_BIRTH_YEAR, ASSUMED_HEIGHT_CM
from app.storage import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


def _insert_weighin(conn, weighin_id, date, **fields):
    columns = {
        "id": weighin_id,
        "date": date,
        "content_hash": "sha256:x",
        "raw_path": f"{date}.md",
        "received_at": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        "weight_kg": 0,
        "bmi": 0,
        "body_fat_percent": 0,
        "lean_mass_percent": 0,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO scale_weighins ({cols}) VALUES ({placeholders})", list(columns.values())
    )
    conn.commit()


def _insert_readiness_event(conn, event_id, measured_at, **fields):
    columns = {
        "id": event_id,
        "measured_at": measured_at,
        "content_hash": "sha256:x",
        "raw_path": f"{event_id}.md",
        "received_at": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        "steps_avg_per_day": None,
        "steps_days_spanned": None,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO readiness_events ({cols}) VALUES ({placeholders})", list(columns.values())
    )
    conn.commit()


def _insert_session(conn, session_id, date, **fields):
    columns = {
        "id": session_id,
        "date": date,
        "content_hash": "sha256:x",
        "raw_path": f"{date}.md",
        "received_at": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        "resting_hr": 0,
        "session_calories": 0,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({placeholders})", list(columns.values()))
    conn.commit()


def test_no_weight_reading_returns_none(conn):
    assert estimated_maintenance_calories(conn, today="2026-08-09") is None


def test_bmr_only_matches_mifflin_st_jeor(conn):
    """With no steps and no recent sessions, mid should equal BMR * 1.10
    (TEF, no NEAT/EAT) — pins the formula against a hand-computed value so a
    future accidental coefficient change is caught."""
    _insert_weighin(conn, "w1", "2026-08-01", weight_kg=80.0)

    result = estimated_maintenance_calories(conn, today="2026-08-09")

    assert result is not None
    age = 2026 - ASSUMED_BIRTH_YEAR
    expected_bmr = 10 * 80.0 + 6.25 * ASSUMED_HEIGHT_CM - 5 * age + 5
    assert result.bmr == pytest.approx(expected_bmr)
    assert result.bmr_formula == "mifflin-st-jeor"
    assert result.neat == 0.0
    assert result.eat == 0.0
    assert result.mid == pytest.approx(expected_bmr * 1.10)
    assert result.low == pytest.approx(result.mid * 0.85)
    assert result.high == pytest.approx(result.mid * 1.15)
    assert result.low < result.mid < result.high


def test_katch_mcardle_used_when_real_bia_reading_present(conn):
    """A real BIA reading (body_fat_percent > 0, same convention as
    scale_weekly_trend's leanMassPercent bucket) should switch the BMR
    formula to Katch-McArdle (lean-mass-based), not the height/age/sex
    Mifflin-St Jeor fallback."""
    _insert_weighin(
        conn, "w1", "2026-08-01", weight_kg=80.0, body_fat_percent=15.0, lean_mass_percent=85.0
    )

    result = estimated_maintenance_calories(conn, today="2026-08-09")

    assert result is not None
    lean_mass_kg = 80.0 * 0.85
    expected_bmr = 370 + 21.6 * lean_mass_kg
    assert result.bmr == pytest.approx(expected_bmr)
    assert result.bmr_formula == "katch-mcardle"


def test_mifflin_fallback_when_bia_reading_missing(conn):
    """body_fat_percent<=0/missing means "no real BIA reading" (same
    convention as scale_weekly_trend) — must fall back to Mifflin-St Jeor,
    not silently compute a bogus Katch-McArdle from a zero lean mass."""
    _insert_weighin(conn, "w1", "2026-08-01", weight_kg=80.0, body_fat_percent=0)

    result = estimated_maintenance_calories(conn, today="2026-08-09")

    assert result is not None
    assert result.bmr_formula == "mifflin-st-jeor"


def test_uses_most_recent_weight_not_average(conn):
    _insert_weighin(conn, "w1", "2026-07-01", weight_kg=90.0)
    _insert_weighin(conn, "w2", "2026-08-01", weight_kg=80.0)

    result = estimated_maintenance_calories(conn, today="2026-08-09")

    assert result.weight_kg == 80.0


def test_steps_contribute_to_neat(conn):
    _insert_weighin(conn, "w1", "2026-08-01", weight_kg=80.0)
    _insert_readiness_event(conn, "r1", "2026-08-05T07:00:00", steps_avg_per_day=10000.0)

    result = estimated_maintenance_calories(conn, today="2026-08-09")

    assert result.steps_avg_per_day == 10000.0
    assert result.neat > 0
    assert result.mid > result.bmr  # NEAT+TEF pushed the total above bare BMR


def test_recent_session_calories_contribute_to_eat(conn):
    _insert_weighin(conn, "w1", "2026-08-01", weight_kg=80.0)
    _insert_session(conn, "s1", "2026-08-08", session_calories=700)

    result = estimated_maintenance_calories(conn, today="2026-08-09")

    # Spread across all 7 days of the window, not just the training day.
    assert result.eat == pytest.approx(700 / 7.0)
    assert result.session_calories_per_day == pytest.approx(700 / 7.0)


def test_old_session_outside_window_does_not_contribute(conn):
    _insert_weighin(conn, "w1", "2026-08-01", weight_kg=80.0)
    _insert_session(conn, "s1", "2026-07-01", session_calories=700)

    result = estimated_maintenance_calories(conn, today="2026-08-09")

    assert result.eat == 0.0


def test_zero_session_calories_excluded_same_as_other_trends(conn):
    """session_calories=0 means "field omitted" per the sender's space-
    saving convention (docs/sync-ingestion/SPEC.md), same reasoning as
    _session_metric_trend's `> 0` filter — must not be averaged in as a
    real zero-calorie session."""
    _insert_weighin(conn, "w1", "2026-08-01", weight_kg=80.0)
    _insert_session(conn, "s1", "2026-08-08", session_calories=0)

    result = estimated_maintenance_calories(conn, today="2026-08-09")

    assert result.eat == 0.0
