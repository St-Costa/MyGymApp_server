"""Covers the "Testing checklist" items in docs/sync-ingestion/SPEC.md that
apply to the live POST /v1/sessions endpoint."""

import sqlite3

from tests.conftest import SESSION_MD, auth_headers, content_hash_of, post_session


def _db_conn(app_client):
    import app.config

    return sqlite3.connect(app.config.DB_PATH)


def test_valid_session_is_stored(app_client) -> None:
    response = post_session(app_client)
    assert response.status_code == 200
    body = response.json()
    assert body == {"sessionId": "3c4d5e6f", "status": "stored"}

    import app.config

    raw_path = app.config.RAW_DIR / "2025/04/2025-04-15_rt-b2c3d4e5_3c4d5e6f.md"
    assert raw_path.read_bytes() == SESSION_MD.encode()

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT id, total_tonnage, notes FROM sessions WHERE id = ?", ("3c4d5e6f",)
    ).fetchone()
    assert row == ("3c4d5e6f", 12450.0, "Optional free-text session notes go here.")


def test_duplicate_delivery_is_a_safe_noop(app_client) -> None:
    first = post_session(app_client)
    assert first.json()["status"] == "stored"

    second = post_session(app_client)
    assert second.status_code == 200
    assert second.json() == {"sessionId": "3c4d5e6f", "status": "duplicate"}

    conn = _db_conn(app_client)
    count = conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", ("3c4d5e6f",)).fetchone()[0]
    assert count == 1


def test_hash_mismatch_writes_nothing(app_client) -> None:
    response = post_session(app_client, content_hash="sha256:" + "0" * 64)
    assert response.status_code == 422

    import app.config

    assert not app.config.RAW_DIR.exists() or list(app.config.RAW_DIR.rglob("*.md")) == []

    conn = _db_conn(app_client)
    count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert count == 0


def test_missing_bearer_token_is_rejected(app_client) -> None:
    response = post_session(app_client, headers={})
    assert response.status_code == 401


def test_wrong_bearer_token_is_rejected(app_client) -> None:
    response = post_session(app_client, headers={"Authorization": "Bearer wrong-secret"})
    assert response.status_code == 401


def test_omitted_numeric_fields_default_to_zero(app_client) -> None:
    minimal_md = (
        b"---\n"
        b"id: 00000001\n"
        b"date: 2025-04-15\n"
        b"routineId: rt-00000001\n"
        b"routineName: Quick Session\n"
        b"startedAt: 2025-04-15T09:00:00\n"
        b"completedAt: 2025-04-15T09:30:00\n"
        b"totalTonnage: 100.0\n"
        b"exercises: []\n"
        b"---\n"
    )

    response = post_session(
        app_client,
        minimal_md,
        session_id="00000001",
        rel_path="2025/04/2025-04-15_rt-00000001_00000001.md",
    )
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        """SELECT ecg_beats, ecg_duration_sec, ecg_session_rmssd, resting_hr, hrr60s, sdnn
           FROM sessions WHERE id = ?""",
        ("00000001",),
    ).fetchone()
    assert row == (0, 0, 0.0, 0, 0, 0.0)
    assert None not in row


def test_updated_session_overwrites_and_replaces_child_rows(app_client) -> None:
    first = post_session(app_client)
    assert first.json()["status"] == "stored"

    updated_md = SESSION_MD.replace("totalTonnage: 12450.0", "totalTonnage: 9999.0").encode()
    second = post_session(app_client, updated_md, content_hash=content_hash_of(updated_md))
    assert second.status_code == 200
    assert second.json() == {"sessionId": "3c4d5e6f", "status": "updated"}

    conn = _db_conn(app_client)
    rows = conn.execute(
        "SELECT id, total_tonnage FROM sessions WHERE id = ?", ("3c4d5e6f",)
    ).fetchall()
    assert rows == [("3c4d5e6f", 9999.0)]

    set_count = conn.execute(
        "SELECT COUNT(*) FROM exercise_sets WHERE session_id = ?", ("3c4d5e6f",)
    ).fetchone()[0]
    assert set_count == 2  # same exercise re-inserted once, not accumulated


def test_malformed_envelope_json_returns_400(app_client) -> None:
    response = app_client.post(
        "/v1/sessions",
        data={"envelope": "{not valid json"},
        files={"file": ("session.md", SESSION_MD.encode(), "text/markdown")},
        headers=auth_headers(),
    )
    assert response.status_code == 400
    assert "error" in response.json()


def test_envelope_missing_required_field_returns_400(app_client) -> None:
    import json

    envelope = {
        "sessionId": "3c4d5e6f",
        # relPath missing
        "contentHash": content_hash_of(SESSION_MD.encode()),
        "appVersion": "1.0-test",
        "clientSentAt": "2025-04-15T10:05:42Z",
    }
    response = app_client.post(
        "/v1/sessions",
        data={"envelope": json.dumps(envelope)},
        files={"file": ("session.md", SESSION_MD.encode(), "text/markdown")},
        headers=auth_headers(),
    )
    assert response.status_code == 400
