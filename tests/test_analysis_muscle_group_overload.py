"""Weekly muscle-group volume/frequency and per-exercise progressive-overload
verdict — GUI-only additions, not in ANALYSIS_SPEC.md, added on user request.
See app.analysis's module comments above muscle_group_weekly_trend and
progressive_overload_verdict for the literature/reasoning behind the
thresholds exercised here."""

import sqlite3

import pytest

from app.analysis import (
    exercise_ids_trained_this_week,
    muscle_group_weekly_trend,
    progressive_overload_verdict,
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


def test_muscle_group_weekly_trend_matches_exercise_type_case_insensitively(conn):
    # Regression: real synced data observed lowercase 'forza'/'stretch', not
    # SPEC.md's documented 'FORZA'/'STRETCH' — a case-sensitive filter here
    # would silently count zero sets for every real session.
    _insert_session(conn, "s1", "2026-08-03")
    for i in range(6):
        _insert_set(
            conn, "s1", "ex-a", i, reps=8, weight=80, bodypart="chest", exercise_type="forza"
        )
    conn.commit()

    trend = muscle_group_weekly_trend(conn, today="2026-08-09")
    week = next(w for w in trend["chest"] if w.week_start == "2026-08-03")
    assert week.set_count == 6


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


# today="2026-08-01" (Saturday) -> current week Mon 2026-07-27..Sun
# 2026-08-02, prior week Mon 2026-07-20..Sun 2026-07-26, week before that
# Mon 2026-07-13..Sun 2026-07-19.
_TODAY = "2026-08-01"


def test_progressive_overload_verdict_up_healthy_moderate_weekly_rise(conn):
    # Prior week (2026-07-20) avg 80, current week (2026-07-27) avg 82 -> +2.5%/week.
    _insert_session(conn, "s1", "2026-07-20")
    _insert_set(conn, "s1", "ex-a", 0, reps=5, weight=80)
    _insert_session(conn, "s2", "2026-07-27")
    _insert_set(conn, "s2", "ex-a", 0, reps=5, weight=82)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today=_TODAY)
    assert verdict.e1rm.verdict == "UP_HEALTHY"
    assert verdict.e1rm.weeks_ago == 1


def test_progressive_overload_verdict_up_too_fast_flags_implausible_jump(conn):
    # Prior week avg 80, current week avg 92 -> +15%/week, well above the band.
    _insert_session(conn, "s1", "2026-07-20")
    _insert_set(conn, "s1", "ex-a", 0, reps=5, weight=80)
    _insert_session(conn, "s2", "2026-07-27")
    _insert_set(conn, "s2", "ex-a", 0, reps=5, weight=92)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today=_TODAY)
    assert verdict.e1rm.verdict == "UP_TOO_FAST"


def test_progressive_overload_verdict_down_when_e1rm_falls(conn):
    _insert_session(conn, "s1", "2026-07-20")
    _insert_set(conn, "s1", "ex-a", 0, reps=5, weight=100)
    _insert_session(conn, "s2", "2026-07-27")
    _insert_set(conn, "s2", "ex-a", 0, reps=5, weight=90)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today=_TODAY)
    assert verdict.e1rm.verdict == "DOWN"


def test_progressive_overload_verdict_stalled_within_tolerance(conn):
    _insert_session(conn, "s1", "2026-07-20")
    _insert_set(conn, "s1", "ex-a", 0, reps=5, weight=100)
    _insert_session(conn, "s2", "2026-07-27")
    _insert_set(conn, "s2", "ex-a", 0, reps=5, weight=100.02)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today=_TODAY)
    assert verdict.e1rm.verdict == "STALLED"


def test_progressive_overload_verdict_skips_empty_weeks_to_find_prior_data(conn):
    # No session in the week immediately before current (2026-07-20) — the
    # last week with data is 2 weeks back (2026-07-13). weeks_ago must be 2,
    # and the rate must be normalized by 2 weeks, not 1.
    _insert_session(conn, "s1", "2026-07-13")
    _insert_set(conn, "s1", "ex-a", 0, reps=5, weight=80)
    _insert_session(conn, "s2", "2026-07-27")
    _insert_set(conn, "s2", "ex-a", 0, reps=5, weight=84)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today=_TODAY)
    assert verdict.e1rm.weeks_ago == 2
    assert verdict.e1rm.prior_week_start == "2026-07-13"
    # +5% over 2 weeks = +2.5%/week, not +5%/week.
    assert verdict.e1rm.percent_per_week == pytest.approx(2.5, abs=0.01)


def test_progressive_overload_verdict_averages_multiple_sessions_within_a_week(conn):
    # Two sessions in the current week (e.g. one heavy/primary, one
    # lighter/secondary) must be averaged, not just the last one used.
    # e1RM (Epley, reps=5): weight=90 -> 105.0, weight=70 -> 81.67; average 93.33.
    _insert_session(conn, "s1", "2026-07-20")
    _insert_set(conn, "s1", "ex-a", 0, reps=5, weight=80)
    _insert_session(conn, "s2", "2026-07-27")
    _insert_set(conn, "s2", "ex-a", 0, reps=5, weight=90)  # heavy/primary
    _insert_session(conn, "s3", "2026-07-28")
    _insert_set(conn, "s3", "ex-a", 0, reps=5, weight=70)  # light/secondary
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today=_TODAY)
    # Average of the two e1RMs, not just 81.67 (the last session's alone).
    assert verdict.e1rm.current_week_avg == pytest.approx(93.33, abs=0.01)


def test_progressive_overload_verdict_no_data_this_week(conn):
    # Exercise has history but nothing in the current calendar week.
    _insert_session(conn, "s1", "2026-07-20")
    _insert_set(conn, "s1", "ex-a", 0, reps=5, weight=80)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today=_TODAY)
    assert verdict.e1rm.verdict == "INSUFFICIENT_DATA"
    assert verdict.e1rm.current_week_avg is None


def test_progressive_overload_verdict_no_prior_week_to_compare(conn):
    # Only the current week has data — nothing in the trailing 12 weeks before it.
    _insert_session(conn, "s1", "2026-07-27")
    _insert_set(conn, "s1", "ex-a", 0, reps=5, weight=80)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today=_TODAY)
    assert verdict.e1rm.verdict == "INSUFFICIENT_DATA"
    assert verdict.e1rm.current_week_avg is not None
    assert verdict.e1rm.prior_week_avg is None


def test_progressive_overload_verdict_no_data_at_all(conn):
    verdict = progressive_overload_verdict(conn, "ex-nonexistent", today=_TODAY)
    assert verdict.e1rm.verdict == "INSUFFICIENT_DATA"
    assert verdict.tonnage.verdict == "INSUFFICIENT_DATA"
    assert verdict.point_count == 0
    assert verdict.e1rm.current_week_avg is None


def test_progressive_overload_verdict_tonnage_independent_of_e1rm(conn):
    # Same top weight both weeks (e1RM flat) but rising set count -> tonnage
    # should trend UP while e1RM stays STALLED, demonstrating the two
    # verdicts can genuinely disagree rather than being redundant.
    _insert_session(conn, "s1", "2026-07-20")
    for j in range(3):
        _insert_set(conn, "s1", "ex-a", j, reps=8, weight=80)
    _insert_session(conn, "s2", "2026-07-27")
    for j in range(6):
        _insert_set(conn, "s2", "ex-a", j, reps=8, weight=80)
    conn.commit()

    verdict = progressive_overload_verdict(conn, "ex-a", today=_TODAY)
    assert verdict.e1rm.verdict == "STALLED"
    assert verdict.tonnage.verdict in ("UP_HEALTHY", "UP_TOO_FAST")
    assert verdict.tonnage.current_week_avg > verdict.tonnage.prior_week_avg


# ---------------------------------------------------------------------------
# exercise_ids_trained_this_week
# ---------------------------------------------------------------------------


def test_exercise_ids_trained_this_week_calendar_week_not_rolling(conn):
    # today="2026-08-01" (Saturday) -> current week is Mon 2026-07-27..Sun
    # 2026-08-02. A session on 2026-07-26 (the Sunday just before) is outside
    # that calendar week even though it's within a trailing 7 days of today —
    # this is the "week means Monday-anchored, not rolling 7 days" contract.
    _insert_session(conn, "s_prev_week", "2026-07-26")
    _insert_set(conn, "s_prev_week", "ex-prev", 0, reps=8, weight=80)
    _insert_session(conn, "s_this_week", "2026-07-27")
    _insert_set(conn, "s_this_week", "ex-this", 0, reps=8, weight=80)
    conn.commit()

    trained = exercise_ids_trained_this_week(conn, today="2026-08-01")
    assert trained == {"ex-this"}


def test_exercise_ids_trained_this_week_no_sessions_returns_empty_set(conn):
    assert exercise_ids_trained_this_week(conn, today="2026-08-01") == set()
