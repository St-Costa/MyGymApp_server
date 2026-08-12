"""Weekly time-series versions of the cardio metrics/rhythm counters and the
per-exercise history series — GUI-only additions, not themselves in
ANALYSIS_SPEC.md, built from the same underlying data/helpers the spec's
own functions already use."""

import sqlite3

import pytest

from app.analysis import (
    all_cardio_metrics_weekly_trend,
    ecg_advanced_metrics_weekly_trend,
    estimated_vo2max,
    exercise_history,
    hr_zones_weekly_trend,
    latest_readiness,
    nearest_scale_weight_kg,
    readiness_daily_trend,
    resting_hr_7day_average,
    resting_hr_7day_min,
)
from app.storage import (
    EcgAnalysisResult,
    ParsedEcg,
    init_db,
    upsert_ecg_analysis,
    upsert_parsed_ecg,
)


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


def _insert_scale_weighin(conn, weighin_id, date, **fields):
    columns = {
        "id": weighin_id,
        "date": date,
        "content_hash": "sha256:x",
        "raw_path": f"{weighin_id}.md",
        "received_at": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO scale_weighins ({cols}) VALUES ({placeholders})", list(columns.values())
    )
    conn.commit()


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


def test_readiness_daily_trend_includes_raw_numbers_behind_the_category(conn):
    _insert_readiness(
        conn,
        "r1",
        "2026-08-01T07:00:00",
        readiness="GOOD",
        ln_rmssd=4.3,
        resting_hr=58,
        vo2max=45.5,
    )

    trend = readiness_daily_trend(conn, today="2026-08-01")
    assert trend[0].ln_rmssd == 4.3
    assert trend[0].resting_hr == 58
    assert trend[0].vo2max == 45.5


def test_readiness_daily_trend_zero_numbers_are_unmeasured_not_real_readings(conn):
    """READINESS_SPEC.md: 0/0.0 is the phone's "omitted means unmeasured"
    convention, same as sessions' numeric fields — must surface as None,
    not a misleadingly real zero reading."""
    _insert_readiness(
        conn,
        "r1",
        "2026-08-01T07:00:00",
        readiness="NORMAL",
        ln_rmssd=0.0,
        resting_hr=0,
        vo2max=0.0,
    )

    trend = readiness_daily_trend(conn, today="2026-08-01")
    assert trend[0].ln_rmssd is None
    assert trend[0].resting_hr is None
    assert trend[0].vo2max is None


def _insert_ecg_analysis(conn, session_id, **fields):
    """Writes an ecg_recordings header row (satisfies ecg_advanced_metrics_
    weekly_trend's JOIN target) plus an ecg_analysis row with the given
    metric values — mirrors tests/test_ecg_analysis_storage.py's own
    _insert_unanalyzed_recording helper, extended to also fill in the
    analysis row rather than leaving it queued."""
    upsert_parsed_ecg(
        conn,
        ParsedEcg(id=session_id, sample_rate=130, start_timestamp_ns=0, duration_seconds=300.0),
        content_hash="sha256:" + "0" * 64,
        raw_path=f"ecg/{session_id}.ecg",
        received_at="2026-01-01T00:00:00Z",
    )
    defaults = {
        "avg_hr": None,
        "session_rmssd": None,
        "sdnn": None,
        "pnn50": None,
        "poincare_sd1": None,
        "poincare_sd2": None,
        "poincare_ratio": None,
        "pac_count": None,
        "pause_count": None,
        "irregular_beats": None,
        "afib_suspicion_episodes": None,
        "peak_count": 100,
    }
    upsert_ecg_analysis(
        conn,
        EcgAnalysisResult(session_id=session_id, **{**defaults, **fields}),
        analyzed_at="2026-01-01T00:00:00Z",
    )


def test_ecg_advanced_metrics_weekly_trend_covers_every_metric(conn):
    _insert_session(conn, "s1", "2026-08-03")
    _insert_ecg_analysis(
        conn,
        "s1",
        dfa_alpha1=0.75,
        hrv_lf_hf=2.5,
        sample_entropy=1.2,
        hrr_1min=25.0,
        hrr_2min=40.0,
    )

    trend = ecg_advanced_metrics_weekly_trend(conn, today="2026-08-10")

    def week(key):
        return next(b for b in trend[key] if b.week_start == "2026-08-03")

    assert week("dfaAlpha1").value == 0.75
    assert week("hrvLfHf").value == 2.5
    assert week("sampleEntropy").value == 1.2
    assert week("hrr1min").value == 25.0
    assert week("hrr2min").value == 40.0


def test_ecg_advanced_metrics_weekly_trend_excludes_recordings_without_a_session(conn):
    """The phone's debug-send button uses synthetic debug-* ids (see
    ECG_SPEC.md point 5) with no matching sessions row — the INNER JOIN
    must exclude these from a fitness trend, not error or include them
    with a missing date."""
    _insert_ecg_analysis(conn, "debug-1700000000000", dfa_alpha1=0.5)

    trend = ecg_advanced_metrics_weekly_trend(conn, today="2026-08-10")
    assert all(bucket.value is None for bucket in trend["dfaAlpha1"])


def test_ecg_advanced_metrics_weekly_trend_ignores_none_metrics(conn):
    """A "no result" row (see EcgAnalysisResult's convention) must not be
    treated as 0 in the weekly median — the None-valued weeks should simply
    have no contribution, not silently pull the median toward zero."""
    _insert_session(conn, "s1", "2026-08-03")
    _insert_ecg_analysis(conn, "s1", dfa_alpha1=None)

    trend = ecg_advanced_metrics_weekly_trend(conn, today="2026-08-10")
    assert all(bucket.value is None for bucket in trend["dfaAlpha1"])


def test_ecg_advanced_metrics_weekly_trend_medians_multiple_sessions_per_week(conn):
    _insert_session(conn, "s1", "2026-08-03")
    _insert_session(conn, "s2", "2026-08-04")
    _insert_ecg_analysis(conn, "s1", dfa_alpha1=0.6)
    _insert_ecg_analysis(conn, "s2", dfa_alpha1=0.9)

    trend = ecg_advanced_metrics_weekly_trend(conn, today="2026-08-10")
    week = next(b for b in trend["dfaAlpha1"] if b.week_start == "2026-08-03")
    assert week.value == 0.75  # median(0.6, 0.9)
    assert week.n == 2


def test_hr_zones_weekly_trend_covers_every_zone(conn):
    _insert_session(conn, "s1", "2026-08-03")
    _insert_ecg_analysis(
        conn,
        "s1",
        hr_zone1_min=10.0,
        hr_zone2_min=20.0,
        hr_zone3_min=5.0,
        hr_zone4_min=1.0,
        hr_zone5_min=0.5,
        hr_below_zone1_min=15.0,
    )

    trend = hr_zones_weekly_trend(conn, today="2026-08-10")
    assert set(trend.keys()) == {
        "zone1",
        "zone2",
        "zone3",
        "zone4",
        "zone5",
        "belowZone1",
    }

    def week(key):
        return next(b for b in trend[key] if b.week_start == "2026-08-03")

    assert week("zone1").value == 10.0
    assert week("zone2").value == 20.0
    assert week("zone3").value == 5.0
    assert week("zone4").value == 1.0
    assert week("zone5").value == 0.5
    assert week("belowZone1").value == 15.0


def test_hr_zones_weekly_trend_sums_not_medians_across_sessions(conn):
    """Zone minutes accumulate across a week's sessions — same reasoning as
    the rhythm counters' own sum aggregation, not a per-session average."""
    _insert_session(conn, "s1", "2026-08-03")
    _insert_session(conn, "s2", "2026-08-04")
    _insert_ecg_analysis(conn, "s1", hr_zone2_min=10.0)
    _insert_ecg_analysis(conn, "s2", hr_zone2_min=15.0)

    trend = hr_zones_weekly_trend(conn, today="2026-08-10")
    week = next(b for b in trend["zone2"] if b.week_start == "2026-08-03")
    assert week.value == 25.0  # 10 + 15, not median/average


def test_hr_zones_weekly_trend_excludes_recordings_without_a_session(conn):
    """Same INNER JOIN debug-id exclusion as ecg_advanced_metrics_weekly_trend."""
    _insert_ecg_analysis(conn, "debug-1700000000000", hr_zone2_min=10.0)

    trend = hr_zones_weekly_trend(conn, today="2026-08-10")
    assert all(bucket.value == 0.0 for bucket in trend["zone2"])


def test_hr_zones_weekly_trend_spans_8_weeks_not_12(conn):
    """Own (shorter) window than the other ECG advanced metrics — 6 stacked
    segments per bar reads as clutter past ~8 bars (user decision), unlike
    a single-line 12-week chart. 12-week trends produce 13 Monday-keyed
    buckets over the same 12-week span (the fetch window's own start date
    can land mid-week, adding one partial bucket) — same +1 pattern here,
    so 8 weeks yields 9 buckets, not exactly 8."""
    trend = hr_zones_weekly_trend(conn, today="2026-08-10")
    assert len(trend["zone1"]) == 9


def test_resting_hr_7day_average_averages_trailing_week(conn):
    _insert_readiness(conn, "r1", "2026-08-04T07:00:00Z", resting_hr=58)
    _insert_readiness(conn, "r2", "2026-08-06T07:00:00Z", resting_hr=60)
    _insert_readiness(conn, "r3", "2026-08-09T07:00:00Z", resting_hr=64)

    avg = resting_hr_7day_average(conn, as_of_date="2026-08-10")
    assert avg == pytest.approx((58 + 60 + 64) / 3)


def test_resting_hr_7day_average_excludes_readings_outside_window(conn):
    _insert_readiness(conn, "r1", "2026-08-01T07:00:00Z", resting_hr=70)  # >6 days before as_of
    _insert_readiness(conn, "r2", "2026-08-09T07:00:00Z", resting_hr=60)

    avg = resting_hr_7day_average(conn, as_of_date="2026-08-10")
    assert avg == pytest.approx(60)


def test_resting_hr_7day_average_excludes_zero_as_unmeasured(conn):
    _insert_readiness(conn, "r1", "2026-08-09T07:00:00Z", resting_hr=0)

    assert resting_hr_7day_average(conn, as_of_date="2026-08-10") is None


def test_resting_hr_7day_average_no_readings_returns_none(conn):
    assert resting_hr_7day_average(conn, as_of_date="2026-08-10") is None


def test_nearest_scale_weight_kg_picks_closest_reading(conn):
    _insert_scale_weighin(conn, "2026-08-01", "2026-08-01", weight_kg=79.0)
    _insert_scale_weighin(conn, "2026-08-09", "2026-08-09", weight_kg=80.5)

    assert nearest_scale_weight_kg(conn, as_of_date="2026-08-10") == pytest.approx(80.5)


def test_nearest_scale_weight_kg_prefers_closer_even_if_earlier(conn):
    _insert_scale_weighin(conn, "2026-08-08", "2026-08-08", weight_kg=80.0)
    _insert_scale_weighin(conn, "2026-08-20", "2026-08-20", weight_kg=82.0)

    # 2026-08-14 is 6 days after the 08-08 reading, 6 days before the 08-20
    # reading — exact tie, either is a defensible "nearest"; what matters is
    # it doesn't blow past _NEAREST_WEIGHT_MAX_DAYS or crash.
    result = nearest_scale_weight_kg(conn, as_of_date="2026-08-11")
    assert result == pytest.approx(80.0)  # 3 days away vs. 9 days away


def test_nearest_scale_weight_kg_none_beyond_max_days(conn):
    _insert_scale_weighin(conn, "2026-06-01", "2026-06-01", weight_kg=79.0)

    assert nearest_scale_weight_kg(conn, as_of_date="2026-08-10") is None


def test_nearest_scale_weight_kg_no_data_returns_none(conn):
    assert nearest_scale_weight_kg(conn, as_of_date="2026-08-10") is None


def test_resting_hr_7day_min_takes_minimum_not_average(conn):
    """ANALYSIS_SPEC.md §4.2: hr_rest_for_vo2 is a MIN over the trailing
    week, not an average — deliberately different from
    resting_hr_7day_average (which feeds the unrelated %HRR calculation)."""
    _insert_readiness(conn, "r1", "2026-08-08T07:00:00Z", resting_hr=58)
    _insert_readiness(conn, "r2", "2026-08-09T07:00:00Z", resting_hr=70)
    _insert_readiness(conn, "r3", "2026-08-10T07:00:00Z", resting_hr=62)

    assert resting_hr_7day_min(conn, as_of_date="2026-08-10") == 58


def test_resting_hr_7day_min_falls_back_to_sessions_when_no_readiness(conn):
    _insert_session(conn, "s1", "2026-08-05", resting_hr=64)
    _insert_session(conn, "s2", "2026-08-08", resting_hr=59)

    assert resting_hr_7day_min(conn, as_of_date="2026-08-10") == 59


def test_resting_hr_7day_min_excludes_zero_from_both_sources(conn):
    _insert_readiness(conn, "r1", "2026-08-09T07:00:00Z", resting_hr=0)
    _insert_session(conn, "s1", "2026-08-08", resting_hr=0)

    assert resting_hr_7day_min(conn, as_of_date="2026-08-10") is None


def test_resting_hr_7day_min_no_data_anywhere_returns_none(conn):
    assert resting_hr_7day_min(conn, as_of_date="2026-08-10") is None


def test_estimated_vo2max_uses_three_formula_hr_max_average(conn):
    """15.3 * hr_max / hr_rest, hr_max = mean(Fox/Tanaka/Gulati) for age 26
    (2026 - ASSUMED_BIRTH_YEAR 2000) ≈ 188.97 — same value as the HR-zone
    tests (test_estimated_max_hr_is_the_mean_of_three_published_formulas in
    test_ecg_analysis.py)."""
    _insert_readiness(conn, "r1", "2026-08-09T07:00:00Z", resting_hr=60)

    result = estimated_vo2max(conn, as_of_date="2026-08-10")
    expected_hr_max = (194.0 + 189.8 + 183.12) / 3.0
    assert result == pytest.approx(15.3 * expected_hr_max / 60.0)


def test_estimated_vo2max_none_without_any_resting_hr(conn):
    assert estimated_vo2max(conn, as_of_date="2026-08-10") is None
