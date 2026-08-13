"""§11.3 median/weekly-bucket helpers and §11.4 average_weekly_delta
(docs/sync-ingestion/ANALYSIS_SPEC.md)."""

import sqlite3

import pytest

from app.analysis import (
    average_weekly_delta,
    median,
    resting_hr_weekly_trend,
    scale_weekly_trend,
    steps_avg_per_day_weekly_trend,
    vo2max_4week_trend,
)
from app.storage import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


def test_median_odd_count():
    assert median([1, 3, 2]) == 2


def test_median_even_count():
    assert median([1, 2, 3, 4]) == 2.5


def test_median_empty_is_none():
    assert median([]) is None


def test_average_weekly_delta_uses_only_endpoints():
    # (10 - 4) / (4 - 1) = 2, regardless of the middle values
    assert average_weekly_delta([4, 100, -50, 10]) == 2.0


def test_average_weekly_delta_fewer_than_2_points_is_none():
    assert average_weekly_delta([5]) is None
    assert average_weekly_delta([]) is None


def _insert_session(conn, session_id, date, **fields):
    columns = {
        "id": session_id,
        "date": date,
        "content_hash": "sha256:x",
        "raw_path": f"{date}.md",
        "received_at": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        "resting_hr": 0,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({placeholders})", list(columns.values()))
    conn.commit()


def test_resting_hr_weekly_trend_buckets_by_monday(conn):
    # 2026-08-03 is a Monday; 2026-08-05 (Wed) falls in the same week.
    _insert_session(conn, "s1", "2026-08-03", resting_hr=60)
    _insert_session(conn, "s2", "2026-08-05", resting_hr=64)
    buckets = resting_hr_weekly_trend(conn, today="2026-08-09")
    week = next(b for b in buckets if b.week_start == "2026-08-03")
    assert week.value == 62.0
    assert week.n == 2


def test_resting_hr_weekly_trend_filters_zero_values(conn):
    _insert_session(conn, "s1", "2026-08-03", resting_hr=0)
    buckets = resting_hr_weekly_trend(conn, today="2026-08-09")
    week = next(b for b in buckets if b.week_start == "2026-08-03")
    assert week.value is None
    assert week.n == 0


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


def test_scale_weekly_trend_lean_mass_only_counted_with_real_bia_reading(conn):
    # bodyFatPercent<=0 means no real BIA reading that week -> leanMassPercent excluded.
    _insert_weighin(
        conn, "2026-08-03", "2026-08-03", weight_kg=80, body_fat_percent=0, lean_mass_percent=85
    )
    trends = scale_weekly_trend(conn, today="2026-08-09")
    week = next(b for b in trends["leanMassPercent"] if b.week_start == "2026-08-03")
    assert week.n == 0
    weight_week = next(b for b in trends["weightKg"] if b.week_start == "2026-08-03")
    assert weight_week.value == 80


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


def test_steps_avg_per_day_weekly_trend_buckets_by_monday(conn):
    # 2026-08-03 is a Monday; 2026-08-05 (Wed) falls in the same week.
    _insert_readiness_event(conn, "r1", "2026-08-03T07:00:00", steps_avg_per_day=8000.0)
    _insert_readiness_event(conn, "r2", "2026-08-05T07:00:00", steps_avg_per_day=10000.0)
    buckets = steps_avg_per_day_weekly_trend(conn, today="2026-08-09")
    week = next(b for b in buckets if b.week_start == "2026-08-03")
    assert week.value == 9000.0
    assert week.n == 2


def test_steps_avg_per_day_weekly_trend_excludes_null_but_keeps_zero(conn):
    """NULL means "no data available" and must be dropped from the median;
    a genuinely measured 0 is a real value and must NOT be treated the
    same way — see READINESS_SPEC.md's field notes."""
    _insert_readiness_event(conn, "r1", "2026-08-03T07:00:00", steps_avg_per_day=None)
    _insert_readiness_event(conn, "r2", "2026-08-04T07:00:00", steps_avg_per_day=0.0)
    buckets = steps_avg_per_day_weekly_trend(conn, today="2026-08-09")
    week = next(b for b in buckets if b.week_start == "2026-08-03")
    assert week.value == 0.0
    assert week.n == 1


def test_steps_avg_per_day_weekly_trend_all_null_week_stays_none(conn):
    _insert_readiness_event(conn, "r1", "2026-08-03T07:00:00", steps_avg_per_day=None)
    buckets = steps_avg_per_day_weekly_trend(conn, today="2026-08-09")
    week = next(b for b in buckets if b.week_start == "2026-08-03")
    assert week.value is None
    assert week.n == 0


def test_vo2max_4week_buckets_group_by_4_weeks_not_1(conn):
    today = "2026-08-09"
    _insert_session(conn, "s1", "2026-08-01", vo2max=40)
    _insert_session(conn, "s2", "2026-08-08", vo2max=44)
    buckets = vo2max_4week_trend(conn, today=today)
    non_empty = [b for b in buckets if b.n > 0]
    # Both sessions land in the same 4-week bucket since they're 1 week apart.
    assert len(non_empty) == 1
    assert non_empty[0].n == 2
    assert non_empty[0].value == 42.0


def test_vo2max_4week_trend_prefers_estimated_vo2max_when_resting_hr_available(conn):
    """A session with a resting_hr reading should use app.analysis.
    estimated_vo2max's recompute (Fox/Tanaka/Gulati hr_max) rather than the
    phone's own vo2max (Tanaka-only hr_max) — see vo2max_4week_trend's
    docstring."""
    _insert_session(conn, "s1", "2026-08-08", vo2max=999.0, resting_hr=60)

    buckets = vo2max_4week_trend(conn, today="2026-08-09")
    non_empty = [b for b in buckets if b.n > 0]
    assert len(non_empty) == 1
    # Must NOT be the phone's placeholder 999.0 — the recompute took over.
    assert non_empty[0].value != pytest.approx(999.0)
    assert non_empty[0].value == pytest.approx(15.3 * ((194.0 + 189.8 + 183.12) / 3.0) / 60.0)
