"""§13 session-RPE / Foster session-load trend
(docs/sync-ingestion/ANALYSIS_SPEC.md)."""

import sqlite3

import pytest

from app.analysis import session_load_trend
from app.storage import init_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


def _insert_session(conn, session_id, date, *, session_rpe=None, session_load=None):
    conn.execute(
        """
        INSERT INTO sessions (id, date, content_hash, raw_path, received_at,
                               schema_version, session_rpe, session_load)
        VALUES (?, ?, 'sha256:x', ?, '2026-01-01T00:00:00Z', 1, ?, ?)
        """,
        (session_id, date, f"{date}.md", session_rpe, session_load),
    )
    conn.commit()


def test_no_data_returns_empty_list(conn):
    assert session_load_trend(conn, today="2026-08-08") == []


def test_sessions_without_rpe_are_excluded(conn):
    _insert_session(conn, "s1", "2026-08-01")  # RPE never answered
    assert session_load_trend(conn, today="2026-08-08") == []


def test_sessions_with_rpe_are_included(conn):
    _insert_session(conn, "s1", "2026-08-01", session_rpe=7, session_load=315.0)
    points = session_load_trend(conn, today="2026-08-08")
    assert len(points) == 1
    assert points[0].date == "2026-08-01"
    assert points[0].session_rpe == 7
    assert points[0].session_load == 315.0


def test_sessions_outside_12_week_window_are_excluded(conn):
    _insert_session(conn, "old", "2026-01-01", session_rpe=5, session_load=200.0)
    assert session_load_trend(conn, today="2026-08-08") == []


def test_unparsed_sessions_excluded(conn):
    conn.execute(
        """
        INSERT INTO sessions (id, date, content_hash, raw_path, received_at,
                               schema_version, session_rpe, session_load)
        VALUES ('unparsed', '2026-08-05', 'sha256:x', 'x.md', '2026-01-01T00:00:00Z', 0, 8, 400.0)
        """
    )
    conn.commit()
    assert session_load_trend(conn, today="2026-08-08") == []


def test_results_ordered_chronologically(conn):
    _insert_session(conn, "s2", "2026-08-05", session_rpe=6, session_load=240.0)
    _insert_session(conn, "s1", "2026-08-01", session_rpe=7, session_load=315.0)
    points = session_load_trend(conn, today="2026-08-08")
    assert [p.date for p in points] == ["2026-08-01", "2026-08-05"]
