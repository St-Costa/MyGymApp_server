"""Weekly time-series versions of the cardio metrics/rhythm counters and the
per-exercise history series — GUI-only additions, not themselves in
ANALYSIS_SPEC.md, built from the same underlying data/helpers the spec's
own functions already use."""

import sqlite3

import pytest

from app.analysis import (
    all_cardio_metrics_weekly_trend,
    exercise_history,
    latest_readiness,
    readiness_daily_trend,
)
from app.storage import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


def _insert_session(conn, session_id, date, **fields):
    columns = {
        "id": session_id,
        "date": date,
        "content_hash": "sha256:x",
        "raw_path": f"{date}.md",
        "received_at": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({placeholders})", list(columns.values()))
    conn.commit()


def _insert_set(conn, session_id, exercise_id, set_index, reps, weight, *, exclude=False):
    conn.execute(
        """INSERT INTO exercise_sets
           (session_id, exercise_id, exercise_name, exercise_type, bodypart, set_index,
            reps, weight, exclude_from_tonnage, is_daily)
           VALUES (?, ?, 'Bench Press', 'FORZA', 'chest', ?, ?, ?, ?, 0)""",
        (session_id, exercise_id, set_index, reps, weight, int(exclude)),
    )


def test_all_cardio_metrics_weekly_trend_covers_every_metric_and_rhythm_counter(conn):
    _insert_session(conn, "s1", "2026-08-03", resting_hr=60, afib_suspicion_episodes=1)
    trends = all_cardio_metrics_weekly_trend(conn, today="2026-08-09")

    assert set(trends.keys()) == {
        "restingHr",
        "hrr60s",
        "vo2max",
        "rmssd",
        "sdnn",
        "pnn50",
        "drift",
        "poincareRatio",
        "afibEpisodes",
        "pauses",
        "premature",
        "uneven",
    }
    week = next(b for b in trends["restingHr"] if b.week_start == "2026-08-03")
    assert week.value == 60


def test_all_cardio_metrics_weekly_trend_rhythm_counters_sum_not_average(conn):
    # Two sessions same week, 1 + 2 afib episodes -> weekly total 3, not a median/mean.
    _insert_session(conn, "s1", "2026-08-03", afib_suspicion_episodes=1)
    _insert_session(conn, "s2", "2026-08-04", afib_suspicion_episodes=2)
    trends = all_cardio_metrics_weekly_trend(conn, today="2026-08-09")
    week = next(b for b in trends["afibEpisodes"] if b.week_start == "2026-08-03")
    assert week.value == 3


def test_all_cardio_metrics_weekly_trend_drift_allows_negative_values(conn):
    # drift is the one metric that isn't "positive_only" — negative cardiac
    # drift (HR trending down over a session) is a legitimate value, not
    # noise to filter out like a zero/unset field.
    _insert_session(conn, "s1", "2026-08-03", cardiac_drift_bpm_min=-0.5)
    trends = all_cardio_metrics_weekly_trend(conn, today="2026-08-09")
    week = next(b for b in trends["drift"] if b.week_start == "2026-08-03")
    assert week.value == -0.5


def test_all_cardio_metrics_weekly_trend_excludes_unparsed_sessions(conn):
    _insert_session(conn, "s1", "2026-08-03", resting_hr=999, schema_version=0)
    trends = all_cardio_metrics_weekly_trend(conn, today="2026-08-09")
    week = next(b for b in trends["restingHr"] if b.week_start == "2026-08-03")
    assert week.n == 0


def test_exercise_history_one_point_per_session_chronological(conn):
    _insert_session(conn, "s1", "2026-07-01")
    _insert_set(conn, "s1", "ex-a", 0, reps=8, weight=80)
    _insert_session(conn, "s2", "2026-07-08")
    _insert_set(conn, "s2", "ex-a", 0, reps=6, weight=85)
    conn.commit()

    history = exercise_history(conn, "ex-a", today="2026-08-01")
    assert [p.session_date for p in history] == ["2026-07-01", "2026-07-08"]
    assert history[0].best_set_weight == 80
    assert history[1].best_set_weight == 85


def test_exercise_history_best_set_weight_is_max_not_last(conn):
    _insert_session(conn, "s1", "2026-07-01")
    _insert_set(conn, "s1", "ex-a", 0, reps=8, weight=80)
    _insert_set(conn, "s1", "ex-a", 1, reps=3, weight=100)
    conn.commit()

    history = exercise_history(conn, "ex-a", today="2026-08-01")
    assert history[0].best_set_weight == 100


def test_exercise_history_excludes_exclude_from_tonnage_sets(conn):
    _insert_session(conn, "s1", "2026-07-01")
    _insert_set(conn, "s1", "ex-a", 0, reps=8, weight=80, exclude=True)
    conn.commit()

    history = exercise_history(conn, "ex-a", today="2026-08-01")
    assert history == []


def test_exercise_history_no_data_is_empty_list(conn):
    assert exercise_history(conn, "ex-nonexistent", today="2026-08-01") == []


def _insert_readiness(conn, event_id, measured_at, **fields):
    columns = {
        "id": event_id,
        "measured_at": measured_at,
        "content_hash": "sha256:x",
        "raw_path": f"{event_id}.md",
        "received_at": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO readiness_events ({cols}) VALUES ({placeholders})", list(columns.values())
    )
    conn.commit()


def test_latest_readiness_returns_most_recent_event(conn):
    _insert_readiness(
        conn,
        "r1",
        "2026-08-01T07:00:00",
        readiness="NORMAL",
        ln_rmssd=4.0,
        resting_hr=60,
        vo2max=45.0,
        recommendation="Normal day.",
    )
    _insert_readiness(
        conn,
        "r2",
        "2026-08-05T07:00:00",
        readiness="GOOD",
        ln_rmssd=4.3,
        resting_hr=58,
        vo2max=45.5,
        recommendation="HRV above baseline. Good day to push intensity.",
    )

    result = latest_readiness(conn)
    assert result.measured_at == "2026-08-05T07:00:00"
    assert result.readiness == "GOOD"
    assert result.recommendation == "HRV above baseline. Good day to push intensity."


def test_latest_readiness_no_data_returns_none(conn):
    assert latest_readiness(conn) is None


def test_readiness_daily_trend_returns_one_point_per_day(conn):
    _insert_readiness(conn, "r1", "2026-07-20T07:00:00", readiness="NORMAL")
    _insert_readiness(conn, "r2", "2026-07-25T07:00:00", readiness="GOOD")

    trend = readiness_daily_trend(conn, today="2026-08-01")
    assert [p.date for p in trend] == ["2026-07-20", "2026-07-25"]
    # Plots exactly the phone's own classification — no server-side
    # recalculation (the phone doesn't send the rolling-baseline history
    # needed to reproduce its z-score formula).
    assert [p.readiness for p in trend] == ["NORMAL", "GOOD"]


def test_readiness_daily_trend_excludes_events_outside_window(conn):
    _insert_readiness(conn, "r1", "2026-01-01T07:00:00", readiness="NORMAL")

    trend = readiness_daily_trend(conn, days=30, today="2026-08-01")
    assert trend == []
