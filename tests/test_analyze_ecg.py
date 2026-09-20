"""Covers analyze_ecg.py — the standalone script that drains the
ecg_recordings.analyzed_at IS NULL queue (docs/sync-ingestion/
ECG_ANALYSIS_HANDOFF.md), independent of the network."""

import importlib
import sqlite3
import sys

from tests.conftest import (
    build_ecg_bytes,
    post_ecg,
    post_readiness,
    post_scale_weighin,
    recent_readiness_md,
    recent_scale_weighin_md,
    settle_ecg,
    synthetic_ecg_samples,
)


def _db_conn(app_client):
    import app.config

    return sqlite3.connect(app.config.DB_PATH)


def _load_analyze_ecg():
    sys.modules.pop("analyze_ecg", None)
    import analyze_ecg

    importlib.reload(analyze_ecg)
    return analyze_ecg


def _run(module, *argv) -> int:
    old_argv = sys.argv
    sys.argv = ["analyze_ecg.py", *argv]
    try:
        return module.main()
    finally:
        sys.argv = old_argv


def _upload_without_auto_analysis(app_client, monkeypatch, ecg_bytes) -> None:
    """Simulates a recording that landed before automatic analysis existed
    (or that failed at upload time) — same technique as
    test_ecg_endpoint.py's analysis-failure test, so the queue actually has
    something in it for analyze_ecg.py to pick up."""
    import app.ingestion

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated: analysis unavailable at upload time")

    monkeypatch.setattr(app.ingestion, "analyze_ecg_recording", _boom)
    response = post_ecg(app_client, ecg_bytes)
    assert response.status_code == 200


def test_all_analyzes_every_queued_recording(app_client, monkeypatch) -> None:
    samples = synthetic_ecg_samples(130, 20, 75, seed=1)
    _upload_without_auto_analysis(app_client, monkeypatch, build_ecg_bytes(samples=samples))

    conn = _db_conn(app_client)
    assert conn.execute("SELECT COUNT(*) FROM ecg_analysis").fetchone()[0] == 0

    module = _load_analyze_ecg()
    exit_code = _run(module, "--all")
    assert exit_code == 0

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT avg_hr, peak_count FROM ecg_analysis WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()
    assert row is not None
    assert row[0] is not None
    assert row[1] > 0

    analyzed_at = conn.execute(
        "SELECT analyzed_at FROM ecg_recordings WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]
    assert analyzed_at is not None


def test_all_is_a_noop_once_queue_is_drained(app_client) -> None:
    """A normal upload is auto-analyzed already, so --all should find
    nothing left to do — not re-analyze it."""
    post_ecg(app_client, build_ecg_bytes())

    module = _load_analyze_ecg()
    exit_code = _run(module, "--all")
    assert exit_code == 0


def test_specific_session_id_forces_reanalysis(app_client) -> None:
    """Unlike --all/--stale, passing an explicit session id re-runs analysis
    even if the queue is already drained — useful for manually re-checking
    one recording."""
    post_ecg(app_client, build_ecg_bytes(samples=synthetic_ecg_samples(130, 20, 75, seed=2)))
    settle_ecg()

    conn = _db_conn(app_client)
    before = conn.execute(
        "SELECT analyzed_at FROM ecg_analysis WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]

    module = _load_analyze_ecg()
    exit_code = _run(module, "3c4d5e6f")
    assert exit_code == 0

    conn = _db_conn(app_client)
    after = conn.execute(
        "SELECT avg_hr, analyzed_at FROM ecg_analysis WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()
    assert after[0] is not None
    assert after[1] != before  # re-stamped with a fresh analyzed_at


def test_stale_reanalyzes_only_rows_below_current_schema_version(app_client) -> None:
    post_ecg(app_client, build_ecg_bytes(samples=synthetic_ecg_samples(130, 20, 75, seed=3)))

    conn = _db_conn(app_client)
    with conn:
        conn.execute(
            "UPDATE ecg_analysis SET schema_version = 0 WHERE session_id = ?", ("3c4d5e6f",)
        )

    module = _load_analyze_ecg()
    exit_code = _run(module, "--stale")
    assert exit_code == 0

    conn = _db_conn(app_client)
    schema_version = conn.execute(
        "SELECT schema_version FROM ecg_analysis WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]
    from app.storage import ECG_ANALYSIS_SCHEMA_VERSION

    assert schema_version == ECG_ANALYSIS_SCHEMA_VERSION


def test_stale_reanalysis_picks_up_hrr_zones_from_a_readiness_reading(app_client) -> None:
    """The batch path (analyze_ecg.py) must look up resting HR the same way
    the live endpoint does, via app.ingestion.resting_hr_for_hr_zones — a
    readiness event synced after the original (stale) analysis should still
    switch the re-analysis over to %HRR."""
    post_ecg(app_client, build_ecg_bytes(samples=synthetic_ecg_samples(130, 90, 125, seed=4)))
    settle_ecg()

    conn = _db_conn(app_client)
    with conn:
        conn.execute(
            "UPDATE ecg_analysis SET schema_version = 0 WHERE session_id = ?", ("3c4d5e6f",)
        )
    assert post_readiness(app_client, recent_readiness_md()).status_code == 200

    module = _load_analyze_ecg()
    exit_code = _run(module, "--stale")
    assert exit_code == 0

    conn = _db_conn(app_client)
    used_hrr = conn.execute(
        "SELECT hr_zones_used_hrr FROM ecg_analysis WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]
    assert used_hrr == 1


def test_stale_reanalysis_picks_up_calorie_recompute_from_scale_and_readiness(app_client) -> None:
    """Same batch-path parity as the %HRR test above, for the calorie/TRIMP
    recompute — a readiness reading and scale weigh-in synced after the
    original (stale) analysis should be picked up on re-analysis."""
    post_ecg(app_client, build_ecg_bytes(samples=synthetic_ecg_samples(130, 90, 125, seed=5)))
    settle_ecg()

    conn = _db_conn(app_client)
    with conn:
        conn.execute(
            "UPDATE ecg_analysis SET schema_version = 0 WHERE session_id = ?", ("3c4d5e6f",)
        )
    assert post_readiness(app_client, recent_readiness_md()).status_code == 200
    assert post_scale_weighin(app_client, recent_scale_weighin_md()).status_code == 200

    module = _load_analyze_ecg()
    exit_code = _run(module, "--stale")
    assert exit_code == 0

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT session_calories_ecg, session_trimp_ecg FROM ecg_analysis WHERE session_id = ?",
        ("3c4d5e6f",),
    ).fetchone()
    assert row[0] is not None and row[0] > 0
    assert row[1] is not None and row[1] > 0


def test_missing_raw_file_is_reported_as_a_failure_not_a_crash(app_client, monkeypatch) -> None:
    _upload_without_auto_analysis(app_client, monkeypatch, build_ecg_bytes())

    import app.config

    (app.config.RAW_DIR / "ecg" / "3c4d5e6f.ecg").unlink()

    module = _load_analyze_ecg()
    exit_code = _run(module, "--all")
    assert exit_code == 1  # reported as a failure, not an unhandled exception

    conn = _db_conn(app_client)
    assert conn.execute("SELECT COUNT(*) FROM ecg_analysis").fetchone()[0] == 0
