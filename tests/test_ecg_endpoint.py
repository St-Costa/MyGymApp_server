"""Covers the "Implementation checklist" / receiver-logic items in
docs/sync-ingestion/ECG_SPEC.md for the live POST /v1/ecg endpoint."""

import gzip
import sqlite3
import struct

from tests.conftest import (
    build_ecg_bytes,
    content_hash_of,
    post_ecg,
    post_readiness,
    synthetic_ecg_samples,
)


def _db_conn(app_client):
    import app.config

    return sqlite3.connect(app.config.DB_PATH)


def test_valid_ecg_is_stored(app_client) -> None:
    ecg_bytes = build_ecg_bytes(sample_rate=130)
    response = post_ecg(app_client, ecg_bytes)
    assert response.status_code == 200
    assert response.json() == {"sessionId": "3c4d5e6f", "status": "stored"}

    import app.config

    raw_path = app.config.RAW_DIR / "ecg/3c4d5e6f.ecg"
    # Raw store keeps the *decompressed* bytes, not the gzip transport encoding.
    assert raw_path.read_bytes() == ecg_bytes

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT session_id, sample_rate, duration_seconds FROM ecg_recordings WHERE session_id = ?",
        ("3c4d5e6f",),
    ).fetchone()
    assert row == ("3c4d5e6f", 130, 5.0)


def test_duplicate_ecg_delivery_is_a_safe_noop(app_client) -> None:
    ecg_bytes = build_ecg_bytes()
    first = post_ecg(app_client, ecg_bytes)
    assert first.json()["status"] == "stored"

    second = post_ecg(app_client, ecg_bytes)
    assert second.status_code == 200
    assert second.json() == {"sessionId": "3c4d5e6f", "status": "duplicate"}

    conn = _db_conn(app_client)
    count = conn.execute(
        "SELECT COUNT(*) FROM ecg_recordings WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]
    assert count == 1


def test_resend_with_different_hash_overwrites_not_errors(app_client) -> None:
    """ECG_SPEC.md point 4: a resend for a known sessionId with a *different*
    hash is unexpected (phone captures ECG exactly once) but must still be
    accepted as an overwrite, not rejected."""
    first = post_ecg(app_client, build_ecg_bytes(sample_rate=130))
    assert first.json()["status"] == "stored"

    second = post_ecg(app_client, build_ecg_bytes(sample_rate=125))
    assert second.status_code == 200
    assert second.json() == {"sessionId": "3c4d5e6f", "status": "stored"}

    conn = _db_conn(app_client)
    rows = conn.execute(
        "SELECT session_id, sample_rate FROM ecg_recordings WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchall()
    assert rows == [("3c4d5e6f", 125)]


def test_hash_is_verified_over_compressed_bytes_before_gunzip(app_client) -> None:
    """ECG_SPEC.md point 2: contentHash covers the compressed bytes, not the
    decompressed payload — sending a correct hash for the *decompressed*
    bytes (wrong for what's actually on the wire) must be rejected."""
    ecg_bytes = build_ecg_bytes()
    compressed = gzip.compress(ecg_bytes)
    # Wrong on purpose: hash of the uncompressed payload, not the wire bytes.
    response = post_ecg(
        app_client, compressed_override=compressed, content_hash=content_hash_of(ecg_bytes)
    )
    assert response.status_code == 422

    conn = _db_conn(app_client)
    count = conn.execute("SELECT COUNT(*) FROM ecg_recordings").fetchone()[0]
    assert count == 0


def test_ecg_hash_mismatch_writes_nothing(app_client) -> None:
    response = post_ecg(app_client, content_hash="sha256:" + "0" * 64)
    assert response.status_code == 422

    import app.config

    assert not app.config.RAW_DIR.exists() or list((app.config.RAW_DIR / "ecg").glob("*")) == []

    conn = _db_conn(app_client)
    count = conn.execute("SELECT COUNT(*) FROM ecg_recordings").fetchone()[0]
    assert count == 0


def test_ecg_missing_bearer_token_is_rejected(app_client) -> None:
    response = post_ecg(app_client, headers={})
    assert response.status_code == 401


def test_ecg_wrong_bearer_token_is_rejected(app_client) -> None:
    response = post_ecg(app_client, headers={"Authorization": "Bearer wrong-secret"})
    assert response.status_code == 401


def test_ecg_envelope_missing_encoding_returns_400(app_client) -> None:
    import json

    from tests.conftest import auth_headers

    compressed = gzip.compress(build_ecg_bytes())
    envelope = {
        "sessionId": "3c4d5e6f",
        "relPath": "ecg/3c4d5e6f.ecg",
        "contentHash": content_hash_of(compressed),
        # encoding deliberately omitted
        "appVersion": "1.0-test",
        "clientSentAt": "2025-04-15T10:05:42Z",
    }
    response = app_client.post(
        "/v1/ecg",
        data={"envelope": json.dumps(envelope)},
        files={"file": ("record.ecg.gz", compressed, "application/gzip")},
        headers=auth_headers(),
    )
    assert response.status_code == 400
    assert "error" in response.json()


def test_bad_magic_still_stores_raw_bytes_but_records_parse_failure(app_client) -> None:
    """ECG_SPEC.md step 7: an invalid magic must still result in the raw
    (decompressed) bytes being written and a 200, never a lost upload —
    header parsing is best-effort, same failure-domain split as sessions."""
    garbage = b"NOTANECG" + struct.pack(">i", 130) + struct.pack(">q", 0) + b"\x00" * 20
    response = post_ecg(app_client, garbage)
    assert response.status_code == 200
    assert response.json() == {"sessionId": "3c4d5e6f", "status": "stored"}

    import app.config

    raw_path = app.config.RAW_DIR / "ecg/3c4d5e6f.ecg"
    assert raw_path.read_bytes() == garbage

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT schema_version FROM ecg_recordings WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()
    assert row == (0,)  # UNPARSED_SCHEMA_VERSION sentinel

    failure = conn.execute(
        "SELECT record_type, record_id FROM parse_failures WHERE record_id = ?", ("3c4d5e6f",)
    ).fetchone()
    assert failure == ("ecg_recordings", "3c4d5e6f")


def test_header_only_ecg_file_parses_as_zero_duration(app_client) -> None:
    tiny = b"MYGMECG1" + struct.pack(">i", 130) + struct.pack(">q", 0)  # header only, no samples
    assert len(tiny) == 20  # exactly at the header-size boundary, 0 samples
    response = post_ecg(app_client, tiny)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT sample_rate, duration_seconds FROM ecg_recordings WHERE session_id = ?",
        ("3c4d5e6f",),
    ).fetchone()
    # Zero samples still parses fine (0 samples / 130 Hz = 0.0s) — this isn't
    # the "too short to trust the header at all" case, just an edge duration.
    assert row == (130, 0.0)


def test_truncated_below_header_size_is_stored_but_not_parsed(app_client) -> None:
    """Fewer than 20 bytes (magic+sample_rate+start_timestamp_ns) can't even
    be read as a header — must still be a safe raw-write, never a lost
    upload or a crashed request."""
    truncated = b"MYGMECG1" + struct.pack(">i", 130)  # missing start_timestamp_ns entirely
    response = post_ecg(app_client, truncated)
    assert response.status_code == 200
    assert response.json() == {"sessionId": "3c4d5e6f", "status": "stored"}

    import app.config

    raw_path = app.config.RAW_DIR / "ecg/3c4d5e6f.ecg"
    assert raw_path.read_bytes() == truncated

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT schema_version FROM ecg_recordings WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()
    assert row == (0,)


def test_debug_synthetic_id_is_accepted_with_no_matching_session(app_client) -> None:
    """ECG_SPEC.md point 5: the phone's debug-send button uses a synthetic
    `debug-{epoch millis}` id instead of a real 8-hex session id, and there
    will never be a matching `sessions` row for it. This must be accepted
    like any other sessionId — no format allowlist, no FK to enforce it
    (ecg_recordings.session_id deliberately has none, see app/storage.py)."""
    debug_id = "debug-1754899200000"
    response = post_ecg(app_client, session_id=debug_id, rel_path=f"ecg/{debug_id}.ecg")
    assert response.status_code == 200
    assert response.json() == {"sessionId": debug_id, "status": "stored"}

    import app.config

    raw_path = app.config.RAW_DIR / f"ecg/{debug_id}.ecg"
    assert raw_path.exists()

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT session_id FROM ecg_recordings WHERE session_id = ?", (debug_id,)
    ).fetchone()
    assert row == (debug_id,)
    # No corresponding sessions row — and that's fine, per the spec.
    count = conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (debug_id,)).fetchone()[0]
    assert count == 0


def test_malformed_envelope_json_returns_400(app_client) -> None:
    from tests.conftest import auth_headers

    compressed = gzip.compress(build_ecg_bytes())
    response = app_client.post(
        "/v1/ecg",
        data={"envelope": "{not valid json"},
        files={"file": ("record.ecg.gz", compressed, "application/gzip")},
        headers=auth_headers(),
    )
    assert response.status_code == 400
    assert "error" in response.json()


# ---------------------------------------------------------------------------
# Automatic analysis (docs/sync-ingestion/ECG_ANALYSIS_HANDOFF.md) — runs
# synchronously inside ingest_ecg once the header parses. See
# tests/test_ecg_analysis.py for the analysis math itself; these tests only
# cover that the upload path actually triggers it and never lets it break
# the request.
# ---------------------------------------------------------------------------


def test_valid_ecg_upload_is_analyzed_automatically(app_client) -> None:
    ecg_bytes = build_ecg_bytes(sample_rate=130, samples=synthetic_ecg_samples(130, 20, 75, seed=1))
    response = post_ecg(app_client, ecg_bytes)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT avg_hr, peak_count, schema_version FROM ecg_analysis WHERE session_id = ?",
        ("3c4d5e6f",),
    ).fetchone()
    assert row is not None
    avg_hr, peak_count, schema_version = row
    assert avg_hr is not None
    assert 30 < avg_hr < 220  # any plausible heart rate, sanity bound only
    assert peak_count > 0
    assert schema_version >= 1

    # analyzed_at on the recording itself must be stamped too, so the
    # analyzed_at IS NULL queue doesn't pick this back up.
    analyzed_at = conn.execute(
        "SELECT analyzed_at FROM ecg_recordings WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]
    assert analyzed_at is not None


def test_valid_ecg_upload_gets_advanced_metrics_too(app_client) -> None:
    """docs/sync-ingestion/ECG_ADVANCED_ANALYSIS.md's tier-1 metrics (SQI,
    DFA-alpha1, frequency-domain/nonlinear HRV, HRR) are computed as part of
    the same automatic analysis pass, not a separate step — 90s at a
    regular rhythm clears every minimum-input threshold, including
    frequency-domain HRV's ~60s duration floor."""
    ecg_bytes = build_ecg_bytes(sample_rate=130, samples=synthetic_ecg_samples(130, 90, 75, seed=2))
    response = post_ecg(app_client, ecg_bytes)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        """SELECT kurtosis_sqi, b_sqi, dfa_alpha1, hrv_lf, hrv_hf, hrv_lf_hf,
                  sample_entropy, csi, cvi
           FROM ecg_analysis WHERE session_id = ?""",
        ("3c4d5e6f",),
    ).fetchone()
    assert row is not None
    assert all(value is not None for value in row), row


def test_ecg_upload_uses_hrr_zones_when_a_readiness_reading_exists(app_client) -> None:
    """A resting-HR reading synced within the trailing 7 days should switch
    the automatic analysis to %HRR (Karvonen) — see
    app.analysis.resting_hr_7day_average and app.ingestion.
    resting_hr_for_hr_zones."""
    readiness_response = post_readiness(app_client)
    assert readiness_response.status_code == 200

    ecg_bytes = build_ecg_bytes(
        sample_rate=130, samples=synthetic_ecg_samples(130, 90, 125, seed=3)
    )
    response = post_ecg(app_client, ecg_bytes)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    used_hrr = conn.execute(
        "SELECT hr_zones_used_hrr FROM ecg_analysis WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]
    assert used_hrr == 1


def test_ecg_upload_falls_back_to_hrmax_zones_without_a_readiness_reading(app_client) -> None:
    """No readiness event synced at all -> resting_hr_7day_average returns
    None -> compute_hr_zone_minutes must fall back to %HRmax, not error or
    silently skip the zone columns."""
    ecg_bytes = build_ecg_bytes(
        sample_rate=130, samples=synthetic_ecg_samples(130, 90, 125, seed=3)
    )
    response = post_ecg(app_client, ecg_bytes)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT hr_zones_used_hrr, hr_zone1_min FROM ecg_analysis WHERE session_id = ?",
        ("3c4d5e6f",),
    ).fetchone()
    assert row[0] == 0
    assert row[1] is not None


def test_flat_signal_upload_gets_no_result_not_zeros(app_client) -> None:
    """build_ecg_bytes' default (constant samples, no QRS shape) can't
    produce real R-peaks — the automatic analysis must still run and record
    a "no result" row (None metrics), not silently skip writing one."""
    response = post_ecg(app_client, build_ecg_bytes())
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT avg_hr, pac_count FROM ecg_analysis WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()
    assert row == (None, None)


def test_analysis_failure_does_not_fail_the_upload(app_client, monkeypatch) -> None:
    """A bug/exception in the analysis engine must never take down an
    otherwise-successful upload — the raw file and header row are already
    safely stored by the time analysis runs."""
    import app.ingestion

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated analysis crash")

    monkeypatch.setattr(app.ingestion, "analyze_ecg_recording", _boom)

    response = post_ecg(app_client, build_ecg_bytes())
    assert response.status_code == 200
    assert response.json() == {"sessionId": "3c4d5e6f", "status": "stored"}

    conn = _db_conn(app_client)
    # The recording itself is still there...
    recording = conn.execute(
        "SELECT session_id FROM ecg_recordings WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()
    assert recording is not None
    # ...but analysis never wrote a row, and the recording stays in the
    # unanalyzed queue for a later retry (e.g. via analyze_ecg.py).
    analysis_row = conn.execute(
        "SELECT * FROM ecg_analysis WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()
    assert analysis_row is None
    analyzed_at = conn.execute(
        "SELECT analyzed_at FROM ecg_recordings WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]
    assert analyzed_at is None
