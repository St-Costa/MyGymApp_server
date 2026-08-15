"""§14 Training monotony & strain (Foster et al. 1998) — app.analysis."""

import sqlite3

import pytest

from app.analysis import training_monotony, training_monotony_weekly_trend
from app.storage import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


def _insert_session(conn, session_id, date, *, session_load, schema_version=1):
    conn.execute(
        """
        INSERT INTO sessions (id, date, content_hash, raw_path, received_at,
                               schema_version, session_load)
        VALUES (?, ?, 'sha256:x', ?, '2026-01-01T00:00:00Z', ?, ?)
        """,
        (session_id, date, f"{date}.md", schema_version, session_load),
    )
    conn.commit()


def test_no_data_all_zero_week_returns_none(conn):
    # An empty week is a flat (all-zero) week — stdev is 0, ratio undefined.
    point = training_monotony(conn, today="2026-08-08")
    assert point.monotony is None
    assert point.strain is None
    assert point.daily_loads == [0.0] * 7


def test_rest_days_are_zero_filled_not_skipped(conn):
    # One hard day, six rest days in the trailing 7-day window ending today.
    _insert_session(conn, "s1", "2026-08-08", session_load=700.0)
    point = training_monotony(conn, today="2026-08-08")
    assert point.daily_loads == [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 700.0]
    # mean = 100, population stdev = sqrt(mean((x-100)^2)) over 7 values
    # (six at -100, one at +600) = sqrt((6*10000 + 360000)/7) = sqrt(60000)
    assert point.monotony == pytest.approx(100.0 / 60000**0.5)
    assert point.strain == pytest.approx(700.0 * point.monotony)


def test_identical_daily_loads_yield_none_stdev_zero(conn):
    for i, date in enumerate(
        [
            "2026-08-02",
            "2026-08-03",
            "2026-08-04",
            "2026-08-05",
            "2026-08-06",
            "2026-08-07",
            "2026-08-08",
        ]
    ):
        _insert_session(conn, f"s{i}", date, session_load=300.0)
    point = training_monotony(conn, today="2026-08-08")
    assert point.monotony is None
    assert point.strain is None


def test_varied_loads_give_finite_monotony_and_strain(conn):
    loads = [100.0, 0.0, 200.0, 0.0, 150.0, 0.0, 50.0]
    dates = [
        "2026-08-02",
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
        "2026-08-06",
        "2026-08-07",
        "2026-08-08",
    ]
    for i, (date, load) in enumerate(zip(dates, loads, strict=True)):
        if load > 0:
            _insert_session(conn, f"s{i}", date, session_load=load)
    point = training_monotony(conn, today="2026-08-08")
    assert point.monotony is not None
    assert point.monotony > 0
    assert point.strain == pytest.approx(sum(loads) * point.monotony)


def test_unparsed_sessions_excluded(conn):
    _insert_session(conn, "unparsed", "2026-08-08", session_load=999.0, schema_version=0)
    point = training_monotony(conn, today="2026-08-08")
    assert point.daily_loads == [0.0] * 7


def test_multiple_sessions_same_day_are_summed(conn):
    _insert_session(conn, "s1", "2026-08-08", session_load=100.0)
    _insert_session(conn, "s2a", "2026-08-01", session_load=1.0)  # outside window, ignored
    conn.execute(
        """
        INSERT INTO sessions (id, date, content_hash, raw_path, received_at,
                               schema_version, session_load)
        VALUES ('s3', '2026-08-08', 'sha256:y', 'y.md', '2026-01-01T00:00:00Z', 1, 50.0)
        """
    )
    conn.commit()
    point = training_monotony(conn, today="2026-08-08")
    assert point.daily_loads[-1] == 150.0


def test_weekly_trend_returns_one_point_per_week(conn):
    points = training_monotony_weekly_trend(conn, weeks=4, today="2026-08-08")
    assert len(points) == 4
    # Each point covers a distinct, chronologically increasing week.
    assert [p.week_end for p in points] == sorted(p.week_end for p in points)


def test_weekly_trend_reflects_inserted_load(conn):
    _insert_session(conn, "s1", "2026-08-03", session_load=400.0)
    points = training_monotony_weekly_trend(conn, weeks=2, today="2026-08-08")
    # 2026-08-03 falls in the week ending 2026-08-09 (Monday 2026-08-03 to
    # Sunday 2026-08-09), which is clipped to today (2026-08-08).
    matching = [p for p in points if 400.0 in p.daily_loads]
    assert matching, [p.daily_loads for p in points]
