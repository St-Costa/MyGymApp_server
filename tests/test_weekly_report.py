"""Weekly report content generation (docs/weekly-report/README.md).

Does not re-test app.analysis's own threshold logic (compute_cardio_trend_card,
compute_acwr_trend — already covered by test_analysis_trend.py/test_analysis_acwr.py).
Only verifies that this module consumes those results correctly and applies its
own new logic: exact-week queries, new-PR detection, and the inclusion/silence +
rendering rule.
"""

import sqlite3
from datetime import date

import pytest

from app.storage import init_db
from app.weekly_report import build_weekly_report

WEEK_END = date(2026, 8, 9)  # a Sunday
WEEK_START = date(2026, 8, 3)  # the preceding Monday


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


def _insert_session(
    conn,
    session_id,
    session_date,
    *,
    total_tonnage=0.0,
    vo2max=None,
    resting_hr=None,
    hrr60s=None,
    afib_suspicion_episodes=0,
    ecg_pause_count=0,
    session_rpe=None,
    session_load=None,
):
    conn.execute(
        """
        INSERT INTO sessions (
            id, date, content_hash, raw_path, received_at, schema_version,
            total_tonnage, vo2max, resting_hr, hrr60s,
            afib_suspicion_episodes, ecg_pause_count, session_rpe, session_load
        ) VALUES (?, ?, 'sha256:x', ?, '2026-01-01T00:00:00Z', 1, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            session_date,
            f"{session_date}.md",
            total_tonnage,
            vo2max,
            resting_hr,
            hrr60s,
            afib_suspicion_episodes,
            ecg_pause_count,
            session_rpe,
            session_load,
        ),
    )
    conn.commit()


def _insert_set(conn, session_id, exercise_id, exercise_name, reps, weight):
    conn.execute(
        """
        INSERT INTO exercise_sets (
            session_id, exercise_id, exercise_name, exercise_type, bodypart,
            set_index, reps, weight, exclude_from_tonnage, is_daily, is_bodyweight
        ) VALUES (?, ?, ?, 'FORZA', 'chest', 0, ?, ?, 0, 0, 0)
        """,
        (session_id, exercise_id, exercise_name, reps, weight),
    )
    conn.commit()


def _insert_weighin(conn, weighin_id, weighin_date, *, weight_kg, bmi=None, body_fat=None):
    conn.execute(
        """
        INSERT INTO scale_weighins (
            id, date, content_hash, raw_path, received_at, schema_version,
            weight_kg, bmi, body_fat_percent
        ) VALUES (?, ?, 'sha256:x', ?, '2026-01-01T00:00:00Z', 1, ?, ?, ?)
        """,
        (weighin_id, weighin_date, f"{weighin_date}.md", weight_kg, bmi, body_fat),
    )
    conn.commit()


def test_week_with_no_sessions_is_reported_explicitly_not_silenced(conn):
    report = build_weekly_report(conn, week_end=WEEK_END)
    assert "Nessuna sessione questa settimana" in report.body
    # No exception, no crash on an empty week — raw-first data can legitimately
    # be absent for an entire week.
    assert report.week_start == WEEK_START.isoformat()
    assert report.week_end == WEEK_END.isoformat()


def test_week_with_no_weighins_says_so_not_none(conn):
    _insert_session(conn, "s1", "2026-08-04", total_tonnage=1000.0)
    report = build_weekly_report(conn, week_end=WEEK_END)
    assert "nessuna pesata questa settimana" in report.body.lower()
    assert "None" not in report.body


def test_week_with_no_vo2max_says_so(conn):
    _insert_session(conn, "s1", "2026-08-04", total_tonnage=1000.0)
    report = build_weekly_report(conn, week_end=WEEK_END)
    assert "VO2max: nessun dato questa settimana" in report.body


def test_weight_and_vo2max_always_included_even_when_everything_else_quiet(conn):
    # Stable load across weeks, no arrhythmia, resting HR/HRR in the green
    # band, and steady RPE too (so RPE-ACWR doesn't go GRAY for lack of any
    # data, which would itself count as "notable") -> training load and
    # cardio sections should go quiet, but weight and VO2max must still
    # appear verbatim (never silenced, per the user's explicit priority).
    for i, d in enumerate(f"2026-07-{d:02d}" for d in range(6, 32)):
        _insert_session(
            conn,
            f"c{i}",
            d,
            total_tonnage=1000.0,
            vo2max=45.0,
            resting_hr=55,
            hrr60s=25,
            session_rpe=5,
            session_load=250.0,
        )
    for i, d in enumerate(f"2026-08-{d:02d}" for d in range(1, 10)):
        _insert_session(
            conn,
            f"a{i}",
            d,
            total_tonnage=1000.0,
            vo2max=45.0,
            resting_hr=55,
            hrr60s=25,
            session_rpe=5,
            session_load=250.0,
        )
    _insert_weighin(conn, "2026-08-05", "2026-08-05", weight_kg=78.4, bmi=24.1, body_fat=16.8)

    report = build_weekly_report(conn, week_end=WEEK_END)

    assert "Peso: 78.4 kg" in report.body
    assert "BMI 24.1" in report.body
    assert "VO2max: 45.0 ml/kg/min" in report.body
    assert "Tutto in norma questa settimana" in report.body
    weight_section = next(s for s in report.sections if s.key == "weight")
    vo2max_section = next(s for s in report.sections if s.key == "vo2max")
    training_load_section = next(s for s in report.sections if s.key == "training_load")
    assert weight_section.include is True
    assert vo2max_section.include is True
    assert training_load_section.include is False


def test_red_cardio_metric_is_included_and_raises_priority(conn):
    # Resting HR > 85 is RED per app.analysis's _is_red — should surface as a
    # line in the cardio section and push priority up, without printing the
    # raw BPM number (synthetic-status-only area).
    for i, d in enumerate(f"2026-07-{d:02d}" for d in range(6, 32)):
        _insert_session(conn, f"c{i}", d, total_tonnage=1000.0, resting_hr=60)
    for i, d in enumerate(f"2026-08-{d:02d}" for d in range(1, 10)):
        _insert_session(conn, f"a{i}", d, total_tonnage=1000.0, resting_hr=90)

    report = build_weekly_report(conn, week_end=WEEK_END)

    cardio_section = next(s for s in report.sections if s.key == "cardio_volume")
    assert cardio_section.semaphore == "RED"
    assert "FC a riposo" in cardio_section.headline
    # No raw BPM values printed for this synthetic-status area.
    assert "90" not in cardio_section.headline
    assert report.priority == 5


def test_afib_alert_included_verbatim_regardless_of_other_sections(conn):
    _insert_session(conn, "s1", "2026-08-04", total_tonnage=1000.0, afib_suspicion_episodes=2)
    report = build_weekly_report(conn, week_end=WEEK_END)
    assert any("AFib" in a for a in report.alerts)
    assert any("AFib" in line for line in report.body.splitlines())
    assert report.priority == 5
    assert "warning" in report.tags


def test_new_pr_this_week_is_reported(conn):
    _insert_session(conn, "s_old", "2026-06-01", total_tonnage=500.0)
    _insert_set(conn, "s_old", "ex-1", "Bench Press", 5, 80.0)  # old PR: 400
    _insert_session(conn, "s_new", "2026-08-04", total_tonnage=500.0)
    _insert_set(conn, "s_new", "ex-1", "Bench Press", 5, 100.0)  # new PR: 500

    report = build_weekly_report(conn, week_end=WEEK_END)

    cardio_section = next(s for s in report.sections if s.key == "cardio_volume")
    assert "Nuovo PR: Bench Press" in cardio_section.headline


def test_no_new_pr_when_week_best_below_all_time_pr(conn):
    _insert_session(conn, "s_old", "2026-06-01", total_tonnage=500.0)
    _insert_set(conn, "s_old", "ex-1", "Bench Press", 5, 100.0)  # all-time PR: 500
    _insert_session(conn, "s_new", "2026-08-04", total_tonnage=500.0)
    _insert_set(conn, "s_new", "ex-1", "Bench Press", 5, 80.0)  # below PR: 400

    report = build_weekly_report(conn, week_end=WEEK_END)

    cardio_section = next(s for s in report.sections if s.key == "cardio_volume")
    assert "Nuovo PR" not in cardio_section.headline


def test_session_count_and_tonnage_always_shown(conn):
    _insert_session(conn, "s1", "2026-08-04", total_tonnage=1200.0)
    _insert_session(conn, "s2", "2026-08-06", total_tonnage=800.0)

    report = build_weekly_report(conn, week_end=WEEK_END)

    cardio_section = next(s for s in report.sections if s.key == "cardio_volume")
    assert "2 sessioni" in cardio_section.headline
    assert "2000" in cardio_section.headline


def test_priority_is_normal_when_everything_quiet_and_no_alerts(conn):
    for i, d in enumerate(f"2026-07-{d:02d}" for d in range(6, 32)):
        _insert_session(
            conn,
            f"c{i}",
            d,
            total_tonnage=1000.0,
            vo2max=45.0,
            resting_hr=55,
            hrr60s=25,
            session_rpe=5,
            session_load=250.0,
        )
    for i, d in enumerate(f"2026-08-{d:02d}" for d in range(1, 10)):
        _insert_session(
            conn,
            f"a{i}",
            d,
            total_tonnage=1000.0,
            vo2max=45.0,
            resting_hr=55,
            hrr60s=25,
            session_rpe=5,
            session_load=250.0,
        )

    report = build_weekly_report(conn, week_end=WEEK_END)

    assert report.priority == 3
    assert report.alerts == []
    assert "red_circle" not in report.tags
    assert "yellow_circle" not in report.tags


def test_week_end_defaults_to_last_completed_week_when_not_passed(conn, monkeypatch):
    import app.weekly_report as wr

    class _FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 8, 12)  # a Wednesday

    monkeypatch.setattr(wr, "date_cls", _FixedDate)
    report = build_weekly_report(conn)
    # 2026-08-12 is a Wednesday; the most recently completed week is
    # 2026-08-03 (Mon) .. 2026-08-09 (Sun).
    assert report.week_start == "2026-08-03"
    assert report.week_end == "2026-08-09"
