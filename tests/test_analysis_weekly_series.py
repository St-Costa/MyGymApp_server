"""Weekly time-series versions of the cardio metrics/rhythm counters and the
per-exercise history series — GUI-only additions, not themselves in
ANALYSIS_SPEC.md, built from the same underlying data/helpers the spec's
own functions already use."""

import sqlite3

import pytest

from app.analysis import all_cardio_metrics_weekly_trend, exercise_history
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
