"""Weekly muscle-group volume/frequency and per-exercise progressive-overload
verdict — GUI-only additions, not in ANALYSIS_SPEC.md, added on user request.
See app.analysis's module comments above muscle_group_weekly_trend and
progressive_overload_verdict for the literature/reasoning behind the
thresholds exercised here."""

import sqlite3

import pytest

from app.analysis import muscle_group_weekly_trend, progressive_overload_verdict
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


def _insert_set(
    conn,
    session_id,
    exercise_id,
    set_index,
    reps,
    weight,
    *,
    bodypart="chest",
    exercise_type="FORZA",
    exclude=False,
    is_bodyweight=False,
):
    conn.execute(
        """INSERT INTO exercise_sets
           (session_id, exercise_id, exercise_name, exercise_type, bodypart, set_index,
            reps, weight, exclude_from_tonnage, is_daily, is_bodyweight)
           VALUES (?, ?, 'Some Exercise', ?, ?, ?, ?, ?, ?, 0, ?)""",
        (
            session_id,
            exercise_id,
            exercise_type,
            bodypart,
            set_index,
            reps,
            weight,
            int(exclude),
            int(is_bodyweight),
        ),
    )


# ---------------------------------------------------------------------------
# muscle_group_weekly_trend
# ---------------------------------------------------------------------------


def test_muscle_group_weekly_trend_counts_working_sets_per_bodypart(conn):
    _insert_session(conn, "s1", "2026-08-03")
    for i in range(6):
        _insert_set(conn, "s1", "ex-a", i, reps=8, weight=80, bodypart="chest")
    conn.commit()

    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    week = next(w for w in trend["chest"] if w.week_start == "2026-08-03")
    assert week.set_count == 6
    assert week.session_count == 1


def test_muscle_group_weekly_trend_volume_semaphore_bands(conn):
    # 3 sets -> RED (<5), one session -> also affects frequency but not tested here.
    _insert_session(conn, "s1", "2026-08-03")
    for i in range(3):
        _insert_set(conn, "s1", "ex-a", i, reps=8, weight=80, bodypart="chest")
    conn.commit()
    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    week = next(w for w in trend["chest"] if w.week_start == "2026-08-03")
    assert week.volume_semaphore == "RED"


def test_muscle_group_weekly_trend_volume_semaphore_green_band(conn):
    _insert_session(conn, "s1", "2026-08-03")
    for i in range(12):
        _insert_set(conn, "s1", "ex-a", i, reps=8, weight=80, bodypart="chest")
    conn.commit()
    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    week = next(w for w in trend["chest"] if w.week_start == "2026-08-03")
    assert week.volume_semaphore == "GREEN"


def test_muscle_group_weekly_trend_volume_semaphore_yellow_above_20(conn):
    _insert_session(conn, "s1", "2026-08-03")
    for i in range(25):
        _insert_set(conn, "s1", "ex-a", i, reps=8, weight=80, bodypart="chest")
    conn.commit()
    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    week = next(w for w in trend["chest"] if w.week_start == "2026-08-03")
    assert week.volume_semaphore == "YELLOW"


def test_muscle_group_weekly_trend_frequency_counts_distinct_sessions(conn):
    _insert_session(conn, "s1", "2026-08-03")
    _insert_set(conn, "s1", "ex-a", 0, reps=8, weight=80, bodypart="chest")
    _insert_session(conn, "s2", "2026-08-05")
    _insert_set(conn, "s2", "ex-a", 0, reps=8, weight=80, bodypart="chest")
    conn.commit()

    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    week = next(w for w in trend["chest"] if w.week_start == "2026-08-03")
    assert week.session_count == 2
    assert week.frequency_semaphore == "GREEN"


def test_muscle_group_weekly_trend_frequency_yellow_at_one_session(conn):
    _insert_session(conn, "s1", "2026-08-03")
    _insert_set(conn, "s1", "ex-a", 0, reps=8, weight=80, bodypart="chest")
    conn.commit()

    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    week = next(w for w in trend["chest"] if w.week_start == "2026-08-03")
    assert week.frequency_semaphore == "YELLOW"


def test_muscle_group_weekly_trend_bodyweight_set_counts_as_working(conn):
    # weight=0 but is_bodyweight=True must count, not be filtered as untouched.
    _insert_session(conn, "s1", "2026-08-03")
    for i in range(6):
        _insert_set(conn, "s1", "ex-a", i, reps=15, weight=0, bodypart="core", is_bodyweight=True)
    conn.commit()

    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    week = next(w for w in trend["core"] if w.week_start == "2026-08-03")
    assert week.set_count == 6


def test_muscle_group_weekly_trend_untouched_bodyweight_set_not_counted(conn):
    # weight=0, is_bodyweight=False (never touched) must NOT count as a working set.
    _insert_session(conn, "s1", "2026-08-03")
    _insert_set(conn, "s1", "ex-a", 0, reps=0, weight=0, bodypart="core")
    conn.commit()

    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    assert "core" not in trend or all(w.set_count == 0 for w in trend["core"])


def test_muscle_group_weekly_trend_excludes_exclude_from_tonnage_sets(conn):
    _insert_session(conn, "s1", "2026-08-03")
    for i in range(6):
        _insert_set(conn, "s1", "ex-a", i, reps=8, weight=80, bodypart="chest", exclude=True)
    conn.commit()

    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    assert "chest" not in trend or all(w.set_count == 0 for w in trend["chest"])


def test_muscle_group_weekly_trend_excludes_stretch_type(conn):
    _insert_session(conn, "s1", "2026-08-03")
    _insert_set(conn, "s1", "ex-a", 0, reps=1, weight=0, bodypart="back", exercise_type="STRETCH")
    conn.commit()

    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    assert "back" not in trend or all(w.set_count == 0 for w in trend["back"])


def test_muscle_group_weekly_trend_separates_bodyparts(conn):
    _insert_session(conn, "s1", "2026-08-03")
    _insert_set(conn, "s1", "ex-a", 0, reps=8, weight=80, bodypart="chest")
    _insert_set(conn, "s1", "ex-b", 0, reps=8, weight=40, bodypart="shoulders")
    conn.commit()

    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    assert set(trend.keys()) == {"chest", "shoulders"}


# ---------------------------------------------------------------------------
# progressive_overload_verdict
# ---------------------------------------------------------------------------


def test_progressive_overload_verdict_up_when_e1rm_rises(conn):
    for i, (date, weight) in enumerate(
        [("2026-07-01", 80), ("2026-07-08", 85), ("2026-07-15", 92)]
    ):
        _insert_session(conn, f"s{i}", date)
        _insert_set(conn, f"s{i}", "ex-a", 0, reps=5, weight=weight)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today="2026-08-01")
    assert verdict.verdict == "UP"
    assert verdict.point_count == 3


def test_progressive_overload_verdict_down_when_e1rm_falls(conn):
    for i, (date, weight) in enumerate(
        [("2026-07-01", 100), ("2026-07-08", 95), ("2026-07-15", 88)]
    ):
        _insert_session(conn, f"s{i}", date)
        _insert_set(conn, f"s{i}", "ex-a", 0, reps=5, weight=weight)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today="2026-08-01")
    assert verdict.verdict == "DOWN"


def test_progressive_overload_verdict_flat_within_tolerance(conn):
    for i, (date, weight) in enumerate(
        [("2026-07-01", 100), ("2026-07-08", 100.5), ("2026-07-15", 100.8)]
    ):
        _insert_session(conn, f"s{i}", date)
        _insert_set(conn, f"s{i}", "ex-a", 0, reps=5, weight=weight)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today="2026-08-01")
    assert verdict.verdict == "FLAT"


def test_progressive_overload_verdict_insufficient_data_below_three_points(conn):
    _insert_session(conn, "s1", "2026-07-01")
    _insert_set(conn, "s1", "ex-a", 0, reps=5, weight=80)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today="2026-08-01")
    assert verdict.verdict == "INSUFFICIENT_DATA"
    assert verdict.point_count == 1


def test_progressive_overload_verdict_no_data_at_all(conn):
    verdict = progressive_overload_verdict(conn, "ex-nonexistent", today="2026-08-01")
    assert verdict.verdict == "INSUFFICIENT_DATA"
    assert verdict.point_count == 0
    assert verdict.first_e1rm is None
    assert verdict.last_e1rm is None
