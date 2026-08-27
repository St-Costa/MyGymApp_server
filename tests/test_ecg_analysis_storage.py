"""Storage-layer tests for the ecg_analysis table and its work-queue helpers
(docs/sync-ingestion/ECG_ANALYSIS_HANDOFF.md) — upsert, the analyzed_at IS
NULL queue, and the schema_version staleness query. Analysis math itself is
covered in tests/test_ecg_analysis.py; this file only exercises persistence.

Recordings are inserted directly via upsert_parsed_ecg rather than through
POST /v1/ecg: since app.ingestion.ingest_ecg now runs analysis automatically
on every upload (see tests/test_ecg_endpoint.py's automatic-analysis tests),
going through the live endpoint here would immediately drain the very queue
these tests need populated to exercise get_unanalyzed_ecg_recordings/
upsert_ecg_analysis directly."""

import sqlite3

from app.storage import (
    ECG_ANALYSIS_SCHEMA_VERSION,
    EcgAnalysisResult,
    ParsedEcg,
    get_stale_ecg_analysis,
    get_unanalyzed_ecg_recordings,
    upsert_ecg_analysis,
    upsert_parsed_ecg,
)


def _db_conn(app_client):
    import app.config

    return sqlite3.connect(app.config.DB_PATH)


def _insert_unanalyzed_recording(app_client, session_id: str = "3c4d5e6f") -> None:
    """Writes a bare ecg_recordings row (analyzed_at NULL by construction —
    upsert_parsed_ecg never touches that column) without going through the
    HTTP endpoint's automatic analysis."""
    conn = _db_conn(app_client)
    with conn:
        upsert_parsed_ecg(
            conn,
            ParsedEcg(id=session_id, sample_rate=130, start_timestamp_ns=0, duration_seconds=5.0),
            content_hash="sha256:" + "0" * 64,
            raw_path=f"ecg/{session_id}.ecg",
            received_at="2026-08-11T09:00:00Z",
        )
    conn.close()


def _full_result(session_id: str) -> EcgAnalysisResult:
    return EcgAnalysisResult(
        session_id=session_id,
        avg_hr=72.5,
        session_rmssd=45.0,
        sdnn=50.0,
        pnn50=12.0,
        poincare_sd1=31.8,
        poincare_sd2=61.2,
        poincare_ratio=1.92,
        pac_count=2,
        pause_count=0,
        irregular_beats=4,
        afib_suspicion_episodes=0,
        peak_count=1200,
    )


def test_new_recording_is_in_the_unanalyzed_queue(app_client) -> None:
    _insert_unanalyzed_recording(app_client)
    conn = _db_conn(app_client)
    queue = get_unanalyzed_ecg_recordings(conn)
    assert ("3c4d5e6f", "ecg/3c4d5e6f.ecg") in queue


def test_upsert_ecg_analysis_removes_recording_from_queue(app_client) -> None:
    _insert_unanalyzed_recording(app_client)
    conn = _db_conn(app_client)
    assert get_unanalyzed_ecg_recordings(conn) != []

    with conn:
        upsert_ecg_analysis(conn, _full_result("3c4d5e6f"), analyzed_at="2026-08-11T10:00:00Z")

    assert get_unanalyzed_ecg_recordings(conn) == []

    row = conn.execute(
        """SELECT avg_hr, pac_count, peak_count, schema_version
           FROM ecg_analysis WHERE session_id = ?""",
        ("3c4d5e6f",),
    ).fetchone()
    assert row == (72.5, 2, 1200, ECG_ANALYSIS_SCHEMA_VERSION)

    recording_analyzed_at = conn.execute(
        "SELECT analyzed_at FROM ecg_recordings WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]
    assert recording_analyzed_at == "2026-08-11T10:00:00Z"


def test_upsert_ecg_analysis_stores_none_metrics_as_null(app_client) -> None:
    """A "no result" EcgAnalysisResult (too-short recording) must round-trip
    as SQL NULL, not 0 — 0 would misleadingly look like a real measurement."""
    _insert_unanalyzed_recording(app_client)
    conn = _db_conn(app_client)

    no_result = EcgAnalysisResult(
        session_id="3c4d5e6f",
        avg_hr=None,
        session_rmssd=None,
        sdnn=None,
        pnn50=None,
        poincare_sd1=None,
        poincare_sd2=None,
        poincare_ratio=None,
        pac_count=None,
        pause_count=None,
        irregular_beats=None,
        afib_suspicion_episodes=None,
        peak_count=0,
    )
    with conn:
        upsert_ecg_analysis(conn, no_result, analyzed_at="2026-08-11T10:00:00Z")

    row = conn.execute(
        "SELECT avg_hr, pac_count, peak_count FROM ecg_analysis WHERE session_id = ?",
        ("3c4d5e6f",),
    ).fetchone()
    assert row == (None, None, 0)


def test_upsert_ecg_analysis_is_idempotent_on_session_id(app_client) -> None:
    _insert_unanalyzed_recording(app_client)
    conn = _db_conn(app_client)

    with conn:
        upsert_ecg_analysis(conn, _full_result("3c4d5e6f"), analyzed_at="2026-08-11T10:00:00Z")

    updated = _full_result("3c4d5e6f")
    updated.avg_hr = 80.0
    with conn:
        upsert_ecg_analysis(conn, updated, analyzed_at="2026-08-11T11:00:00Z")

    count = conn.execute(
        "SELECT COUNT(*) FROM ecg_analysis WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]
    assert count == 1

    avg_hr = conn.execute(
        "SELECT avg_hr FROM ecg_analysis WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]
    assert avg_hr == 80.0


def test_get_stale_ecg_analysis_finds_rows_below_current_schema_version(app_client) -> None:
    _insert_unanalyzed_recording(app_client)
    conn = _db_conn(app_client)

    with conn:
        upsert_ecg_analysis(conn, _full_result("3c4d5e6f"), analyzed_at="2026-08-11T10:00:00Z")
    # Force it stale by hand — simulates an ECG_ANALYSIS_SCHEMA_VERSION bump
    # after this row was already analyzed.
    with conn:
        conn.execute(
            "UPDATE ecg_analysis SET schema_version = 0 WHERE session_id = ?", ("3c4d5e6f",)
        )

    stale = get_stale_ecg_analysis(conn)
    assert ("3c4d5e6f", "ecg/3c4d5e6f.ecg") in stale


def test_get_stale_ecg_analysis_excludes_up_to_date_rows(app_client) -> None:
    _insert_unanalyzed_recording(app_client)
    conn = _db_conn(app_client)

    with conn:
        upsert_ecg_analysis(conn, _full_result("3c4d5e6f"), analyzed_at="2026-08-11T10:00:00Z")

    assert get_stale_ecg_analysis(conn) == []


def test_upsert_ecg_analysis_rounds_float_metrics_to_two_decimals(app_client) -> None:
    """The raw HRV/Poincaré/entropy figures come out of the math with long
    floating-point tails carrying no real precision — upsert_ecg_analysis
    caps every stored float at 2 decimals. Integer counts and the None
    sentinels are untouched."""
    _insert_unanalyzed_recording(app_client)
    conn = _db_conn(app_client)

    result = _full_result("3c4d5e6f")
    result.avg_hr = 72.456789
    result.session_rmssd = 45.001
    result.sdnn = 50.6666
    result.sample_entropy = 1.234567
    result.session_calories_ecg = 88.888
    result.pac_count = 3  # int — must stay exact

    with conn:
        upsert_ecg_analysis(conn, result, analyzed_at="2026-08-11T10:00:00Z")

    row = conn.execute(
        """SELECT avg_hr, session_rmssd, sdnn, sample_entropy,
                  session_calories_ecg, pac_count
           FROM ecg_analysis WHERE session_id = ?""",
        ("3c4d5e6f",),
    ).fetchone()
    assert row == (72.46, 45.0, 50.67, 1.23, 88.89, 3)
