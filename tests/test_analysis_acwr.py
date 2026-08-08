"""§12 ACWR (acute:chronic workload ratio) trend
(docs/sync-ingestion/ANALYSIS_SPEC.md)."""

import sqlite3

import pytest

from app.analysis import compute_acwr_trend
from app.storage import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


def _insert_session(conn, session_id, date, total_tonnage):
    conn.execute(
        """
        INSERT INTO sessions (id, date, content_hash, raw_path, received_at,
                               schema_version, total_tonnage)
        VALUES (?, ?, 'sha256:x', ?, '2026-01-01T00:00:00Z', 1, ?)
        """,
        (session_id, date, f"{date}.md", total_tonnage),
    )
    conn.commit()


def test_no_data_gives_gray_semaphore_and_none_ratio(conn):
    trend = compute_acwr_trend(conn, today="2026-08-08")
    assert trend.semaphore == "GRAY"
    assert trend.acwr is None
    assert trend.acute_load == 0.0
    assert trend.chronic_load == 0.0


def test_stable_load_is_green(conn):
    # Same tonnage every day for the full 28-day chronic window (which
    # entirely contains the 8-day acute window) -> acute mean == chronic
    # mean -> acwr == 1.0.
    all_dates = [f"2026-07-{d:02d}" for d in range(11, 32)] + [
        f"2026-08-{d:02d}" for d in range(1, 9)
    ]
    for i, date in enumerate(all_dates):
        _insert_session(conn, f"s{i}", date, 1000.0)

    trend = compute_acwr_trend(conn, today="2026-08-08")
    assert trend.acwr == pytest.approx(1.0)
    assert trend.semaphore == "GREEN"


def test_sudden_spike_in_acute_window_is_red(conn):
    # Light, steady chronic baseline, then a big spike inside the acute
    # window (2026-08-01..08-08) -> acute mean pulled far above chronic mean.
    baseline_dates = [f"2026-07-{d:02d}" for d in range(11, 32)]
    for i, date in enumerate(baseline_dates):
        _insert_session(conn, f"baseline{i}", date, 500.0)
    _insert_session(conn, "spike1", "2026-08-05", 5000.0)
    _insert_session(conn, "spike2", "2026-08-07", 5000.0)

    trend = compute_acwr_trend(conn, today="2026-08-08")
    assert trend.acwr > 1.5
    assert trend.semaphore == "RED"


def test_acwr_boundary_green_upper_bound(conn):
    # today=2026-08-08: acute window is 2026-08-01..08-08 (8 days, inclusive),
    # chronic window is 2026-07-11..08-08 (29 days, inclusive). One session
    # per day throughout: 900 tonnage outside the acute window (21 days),
    # a value inside it (8 days) chosen so acute_mean / chronic_mean lands
    # exactly on 1.3, the green/yellow boundary (inclusive).
    n_baseline, n_acute, baseline_tonnage = 21, 8, 900.0
    # acute/chronic = 1.3, chronic = (n_baseline*baseline + n_acute*acute)/(n_baseline+n_acute)
    acute_tonnage = 1.3 * n_baseline * baseline_tonnage / (n_baseline + n_acute - 1.3 * n_acute)

    baseline_dates = [f"2026-07-{d:02d}" for d in range(11, 32)]
    for i, date in enumerate(baseline_dates):
        _insert_session(conn, f"c{i}", date, baseline_tonnage)
    acute_dates = [f"2026-08-{d:02d}" for d in range(1, 9)]
    for i, date in enumerate(acute_dates):
        _insert_session(conn, f"a{i}", date, acute_tonnage)

    trend = compute_acwr_trend(conn, today="2026-08-08")
    assert trend.acwr == pytest.approx(1.3)
    assert trend.semaphore == "GREEN"


def test_unparsed_sessions_excluded_from_window(conn):
    conn.execute(
        """
        INSERT INTO sessions (id, date, content_hash, raw_path, received_at,
                               schema_version, total_tonnage)
        VALUES ('unparsed', '2026-08-07', 'sha256:x', 'x.md', '2026-01-01T00:00:00Z', 0, 9999.0)
        """
    )
    conn.commit()

    trend = compute_acwr_trend(conn, today="2026-08-08")
    assert trend.acute_load == 0.0
    assert trend.semaphore == "GRAY"


def test_zero_tonnage_sessions_excluded_from_window(conn):
    _insert_session(conn, "zero", "2026-08-07", 0.0)

    trend = compute_acwr_trend(conn, today="2026-08-08")
    assert trend.acute_load == 0.0
    assert trend.semaphore == "GRAY"
