"""§1 tonnage/1RM/PR analysis (docs/sync-ingestion/ANALYSIS_SPEC.md)."""

import sqlite3

import pytest

from app.analysis import (
    ExerciseOccurrence,
    SetLike,
    all_time_tonnage_pr,
    best_estimated_1rm,
    common_tonnage,
    estimate_1rm,
    per_exercise_change,
    recent_heaviest_set_pr,
    tonnage_change_percent,
)
from app.storage import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


# --- §1.4 Epley 1RM ---------------------------------------------------------


def test_estimate_1rm_epley_formula():
    assert estimate_1rm(weight=100, reps=5) == pytest.approx(100 * (1 + 5 / 30.0))


def test_best_estimated_1rm_picks_max_not_sum():
    sets = [SetLike(reps=8, weight=80), SetLike(reps=3, weight=100)]
    result = best_estimated_1rm(sets)
    expected = max(estimate_1rm(80, 8), estimate_1rm(100, 3))
    assert result == pytest.approx(expected)


def test_best_estimated_1rm_ignores_zero_sets():
    sets = [SetLike(reps=0, weight=100), SetLike(reps=5, weight=0)]
    assert best_estimated_1rm(sets) is None


def test_best_estimated_1rm_empty_sets_is_none():
    assert best_estimated_1rm([]) is None


# --- §1.2 common tonnage / tonnage change % --------------------------------


def test_tonnage_change_percent_positive_previous():
    assert tonnage_change_percent(current=110, previous=100) == pytest.approx(10.0)


def test_tonnage_change_percent_none_when_previous_not_positive():
    assert tonnage_change_percent(current=100, previous=0) is None
    assert tonnage_change_percent(current=100, previous=-5) is None


def test_common_tonnage_only_counts_shared_eligible_exercises():
    ex1 = [
        ExerciseOccurrence("ex-a", False, [SetLike(8, 80)]),
        ExerciseOccurrence("ex-b", False, [SetLike(10, 20)]),  # not in session 2
    ]
    ex2 = [
        ExerciseOccurrence("ex-a", False, [SetLike(6, 85)]),
        ExerciseOccurrence("ex-c", False, [SetLike(5, 50)]),  # not in session 1
    ]
    t1, t2 = common_tonnage(ex1, ex2)
    assert t1 == pytest.approx(8 * 80)
    assert t2 == pytest.approx(6 * 85)


def test_common_tonnage_excludes_exclude_from_tonnage_flagged_exercises():
    ex1 = [ExerciseOccurrence("ex-a", True, [SetLike(8, 80)])]
    ex2 = [ExerciseOccurrence("ex-a", True, [SetLike(6, 85)])]
    t1, t2 = common_tonnage(ex1, ex2)
    # excluded from both eligibility sets -> no overlap -> fallback totals,
    # but fallback also filters excludeFromTonnage -> both 0
    assert t1 == 0
    assert t2 == 0


def test_common_tonnage_untouched_exercise_excluded_from_eligibility():
    ex1 = [ExerciseOccurrence("ex-a", False, [])]  # untouched: no sets
    ex2 = [ExerciseOccurrence("ex-a", False, [SetLike(6, 85)])]
    t1, t2 = common_tonnage(ex1, ex2)
    # ex-a not eligible in ex1 (untouched) -> no overlap -> fallback totals
    assert t1 == 0
    assert t2 == pytest.approx(6 * 85)


def test_common_tonnage_no_overlap_falls_back_to_totals():
    ex1 = [ExerciseOccurrence("ex-a", False, [SetLike(8, 80)])]
    ex2 = [ExerciseOccurrence("ex-b", False, [SetLike(6, 85)])]
    t1, t2 = common_tonnage(ex1, ex2)
    assert t1 == pytest.approx(8 * 80)
    assert t2 == pytest.approx(6 * 85)


# --- §1.3 per-exercise change % --------------------------------------------


def test_per_exercise_change_percent():
    result = per_exercise_change("ex-a", prev_sets=[SetLike(8, 80)], curr_sets=[SetLike(8, 88)])
    assert result.change_percent == pytest.approx((704 - 640) / 640 * 100)
    assert result.is_first_time is False


def test_per_exercise_change_is_first_time_when_prev_zero_and_curr_positive():
    result = per_exercise_change("ex-a", prev_sets=[], curr_sets=[SetLike(8, 80)])
    assert result.is_first_time is True
    assert result.change_percent is None


def test_per_exercise_change_none_when_prev_zero():
    result = per_exercise_change("ex-a", prev_sets=[], curr_sets=[])
    assert result.change_percent is None


# --- §1.5 PR detection -------------------------------------------------------


def _insert_session(conn, session_id, date):
    conn.execute(
        """INSERT INTO sessions (id, date, content_hash, raw_path, received_at, schema_version)
           VALUES (?, ?, 'sha256:x', ?, '2026-01-01T00:00:00Z', 1)""",
        (session_id, date, f"{date}.md"),
    )


def _insert_set(
    conn, session_id, exercise_id, set_index, reps, weight, *, exclude=False, is_daily=False
):
    conn.execute(
        """INSERT INTO exercise_sets
           (session_id, exercise_id, exercise_name, exercise_type, bodypart, set_index,
            reps, weight, exclude_from_tonnage, is_daily)
           VALUES (?, ?, 'Bench Press', 'FORZA', 'chest', ?, ?, ?, ?, ?)""",
        (session_id, exercise_id, set_index, reps, weight, int(exclude), int(is_daily)),
    )


def test_all_time_tonnage_pr_picks_best_reps_times_weight(conn):
    _insert_session(conn, "s1", "2026-08-01")
    _insert_set(conn, "s1", "ex-a", 0, reps=8, weight=80)  # 640
    _insert_set(conn, "s1", "ex-a", 1, reps=3, weight=120)  # 360
    _insert_session(conn, "s2", "2026-08-02")
    _insert_set(conn, "s2", "ex-a", 0, reps=5, weight=150)  # 750 <- winner
    conn.commit()

    pr = all_time_tonnage_pr(conn, "ex-a")
    assert pr.reps == 5
    assert pr.weight == 150


def test_all_time_tonnage_pr_excludes_exclude_from_tonnage_sets(conn):
    _insert_session(conn, "s1", "2026-08-01")
    _insert_set(conn, "s1", "ex-a", 0, reps=10, weight=200, exclude=True)
    conn.commit()
    assert all_time_tonnage_pr(conn, "ex-a") is None


def test_all_time_tonnage_pr_no_data_is_none(conn):
    assert all_time_tonnage_pr(conn, "ex-nonexistent") is None


def test_recent_heaviest_set_pr_matches_weight_first_lexicographic(conn):
    _insert_session(conn, "s1", "2026-08-01")
    _insert_set(conn, "s1", "ex-a", 0, reps=10, weight=80)
    _insert_session(conn, "s2", "2026-08-02")
    _insert_set(conn, "s2", "ex-a", 0, reps=3, weight=100)  # heavier weight wins over more reps
    conn.commit()

    pr = recent_heaviest_set_pr(conn, "ex-a", is_daily=False, is_warmup=False)
    assert pr.weight == 100
    assert pr.reps == 3


def test_recent_heaviest_set_pr_respects_is_daily_matching(conn):
    _insert_session(conn, "s1", "2026-08-01")
    _insert_set(conn, "s1", "ex-a", 0, reps=5, weight=999, is_daily=True)
    conn.commit()

    # Looking for non-daily occurrences: the daily-only set must not match.
    pr = recent_heaviest_set_pr(conn, "ex-a", is_daily=False, is_warmup=False)
    assert pr is None
