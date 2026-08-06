"""§11.1/§11.2 cardio trend card + self-diagnosis alerts
(docs/sync-ingestion/ANALYSIS_SPEC.md)."""

import sqlite3

import pytest

from app.analysis import compute_cardio_trend_card
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
        "resting_hr": 0,
        "hrr60s": 0,
        "vo2max": 0,
        "ecg_session_rmssd": 0,
        "sdnn": 0,
        "cardiac_drift_bpm_min": 0,
        "poincare_ratio": 0,
        "afib_suspicion_episodes": 0,
        "ecg_pause_count": 0,
        "ecg_pac_count": 0,
        "ecg_irregular_beats": 0,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({placeholders})", list(columns.values()))
    conn.commit()


def test_no_data_gives_gray_semaphore_and_zero_values(conn):
    card = compute_cardio_trend_card(conn, today="2026-08-05")
    assert card.metrics["restingHr"].semaphore == "GRAY"
    assert card.metrics["restingHr"].current == 0.0
    assert card.metrics["restingHr"].has_enough_data is False


def test_resting_hr_green_within_normal_range(conn):
    for i, hr in enumerate([60, 62, 58]):
        _insert_session(conn, f"s{i}", f"2026-08-0{i + 1}", resting_hr=hr)
    card = compute_cardio_trend_card(conn, today="2026-08-05")
    assert card.metrics["restingHr"].semaphore == "GREEN"


def test_resting_hr_red_when_above_85(conn):
    for i, hr in enumerate([90, 92, 88]):
        _insert_session(conn, f"s{i}", f"2026-08-0{i + 1}", resting_hr=hr)
    card = compute_cardio_trend_card(conn, today="2026-08-05")
    assert card.metrics["restingHr"].semaphore == "RED"


def test_resting_hr_trending_wrong_direction_by_more_than_10_percent_is_red(conn):
    # baseline (14-28 days ago): low resting HR (good). recent (last 14d): much higher.
    _insert_session(conn, "base1", "2026-07-15", resting_hr=60)
    _insert_session(conn, "base2", "2026-07-16", resting_hr=60)
    _insert_session(conn, "recent1", "2026-08-01", resting_hr=80)
    _insert_session(conn, "recent2", "2026-08-02", resting_hr=80)
    card = compute_cardio_trend_card(conn, today="2026-08-05")
    metric = card.metrics["restingHr"]
    assert metric.direction == "UP"  # bad direction for restingHr (good=DOWN)
    # delta=20, baseline_avg=60, 10% of 60=6 -> |delta|>threshold -> RED
    assert metric.semaphore == "RED"


def test_afib_episodes_generate_alert(conn):
    _insert_session(conn, "s1", "2026-08-01", afib_suspicion_episodes=2)
    _insert_session(conn, "s2", "2026-08-02", afib_suspicion_episodes=1)
    card = compute_cardio_trend_card(conn, today="2026-08-05")
    assert card.rhythm.afib_episodes == 3
    assert any("AFib" in a for a in card.alerts)


def test_afib_zero_is_green_rhythm_semaphore(conn):
    card = compute_cardio_trend_card(conn, today="2026-08-05")
    assert card.rhythm.afib_semaphore == "GREEN"


def test_pauses_over_2_generate_alert(conn):
    _insert_session(conn, "s1", "2026-08-01", ecg_pause_count=3)
    card = compute_cardio_trend_card(conn, today="2026-08-05")
    assert any("pauses" in a for a in card.alerts)
    assert card.rhythm.pauses_semaphore == "RED"


def test_few_sessions_in_window_generates_low_data_alert(conn):
    _insert_session(conn, "s1", "2026-08-01")
    card = compute_cardio_trend_card(conn, today="2026-08-05")
    assert any("Only 1 session" in a for a in card.alerts)


def test_resting_hr_up_5_or_more_vs_baseline_generates_fatigue_alert(conn):
    _insert_session(conn, "b1", "2026-07-15", resting_hr=55)
    _insert_session(conn, "b2", "2026-07-16", resting_hr=55)
    _insert_session(conn, "b3", "2026-07-17", resting_hr=55)
    _insert_session(conn, "r1", "2026-08-01", resting_hr=61)
    _insert_session(conn, "r2", "2026-08-02", resting_hr=61)
    _insert_session(conn, "r3", "2026-08-03", resting_hr=61)
    card = compute_cardio_trend_card(conn, today="2026-08-05")
    assert any("Resting HR up" in a for a in card.alerts)


def test_hrr_chronically_low_generates_alert(conn):
    _insert_session(conn, "s1", "2026-08-01", hrr60s=8)
    _insert_session(conn, "s2", "2026-08-02", hrr60s=9)
    _insert_session(conn, "s3", "2026-08-03", hrr60s=10)
    card = compute_cardio_trend_card(conn, today="2026-08-05")
    assert any("HRR chronically" in a for a in card.alerts)


def test_unparsed_sessions_excluded_from_window(conn):
    # schema_version=0 means unparsed (raw-first fallback row) — must not
    # pollute the trend computation with garbage/placeholder values.
    _insert_session(conn, "s1", "2026-08-01", resting_hr=999, schema_version=0)
    card = compute_cardio_trend_card(conn, today="2026-08-05")
    assert card.metrics["restingHr"].current == 0.0
