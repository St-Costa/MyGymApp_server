"""Per-session calories/TRIMP trends (session_calories_trend, session_trimp_trend) —
plain per-session numeric series sharing _session_metric_trend's implementation."""

import sqlite3

import pytest

from app.analysis import session_calories_trend, session_trimp_trend
from app.storage import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


def _insert_session(
    conn, session_id, date, *, session_calories=0, session_trimp=0, schema_version=1
):
    conn.execute(
        """
        INSERT INTO sessions (id, date, content_hash, raw_path, received_at,
                               schema_version, session_calories, session_trimp)
        VALUES (?, ?, 'sha256:x', ?, '2026-01-01T00:00:00Z', ?, ?, ?)
        """,
        (session_id, date, f"{date}.md", schema_version, session_calories, session_trimp),
    )
    conn.commit()


def test_no_data_returns_empty_list(conn):
    assert session_calories_trend(conn, today="2026-08-08") == []
    assert session_trimp_trend(conn, today="2026-08-08") == []


def test_omitted_field_parsed_as_zero_is_excluded(conn):
    # Per SPEC.md, fields omitted on the sender side parse as 0 — that's "no
    # data", not "burned zero calories", so it shouldn't be plotted as a point.
    _insert_session(conn, "s1", "2026-08-01", session_calories=0, session_trimp=0)
    assert session_calories_trend(conn, today="2026-08-08") == []
    assert session_trimp_trend(conn, today="2026-08-08") == []


def test_positive_values_are_included(conn):
    _insert_session(conn, "s1", "2026-08-01", session_calories=412.3, session_trimp=87.5)
    cal_points = session_calories_trend(conn, today="2026-08-08")
    trimp_points = session_trimp_trend(conn, today="2026-08-08")
    assert len(cal_points) == 1
    assert cal_points[0].date == "2026-08-01"
    assert cal_points[0].value == 412.3
    assert len(trimp_points) == 1
    assert trimp_points[0].value == 87.5


def test_sessions_outside_12_week_window_are_excluded(conn):
    _insert_session(conn, "old", "2026-01-01", session_calories=400.0, session_trimp=90.0)
    assert session_calories_trend(conn, today="2026-08-08") == []
    assert session_trimp_trend(conn, today="2026-08-08") == []


def test_unparsed_sessions_excluded(conn):
    _insert_session(
        conn, "unparsed", "2026-08-05", session_calories=400.0, session_trimp=90.0, schema_version=0
    )
    assert session_calories_trend(conn, today="2026-08-08") == []
    assert session_trimp_trend(conn, today="2026-08-08") == []


def test_results_ordered_chronologically(conn):
    _insert_session(conn, "s2", "2026-08-05", session_calories=500.0, session_trimp=100.0)
    _insert_session(conn, "s1", "2026-08-01", session_calories=400.0, session_trimp=80.0)
    cal_points = session_calories_trend(conn, today="2026-08-08")
    assert [p.date for p in cal_points] == ["2026-08-01", "2026-08-05"]
