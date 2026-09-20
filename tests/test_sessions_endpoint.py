"""Covers the "Testing checklist" items in docs/sync-ingestion/SPEC.md that
apply to the live POST /v1/sessions endpoint."""

import sqlite3

from tests.conftest import (
    SESSION_MD,
    auth_headers,
    content_hash_of,
    post_session,
    post_sessions_bulk,
)


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


def test_cardio_exercise_lands_in_exercise_sets_with_reps_weight_null(app_client) -> None:
    # docs/sync-ingestion/CARDIO_BLOCKS_SPEC.md
    md = SESSION_MD.replace(
        "exercises:\n"
        "  - exerciseId: ex-a1b2c3d4\n"
        "    exerciseName: Bench Press\n"
        "    type: FORZA\n"
        "    bodypart: chest\n"
        "    excludeFromTonnage: true\n"
        "    isDaily: true\n"
        "    sets:\n"
        "      - reps: 8\n"
        "        weight: 80.0\n"
        "      - reps: 6\n"
        "        weight: 85.0\n",
        "exercises:\n"
        "  - exerciseId: ex-c9d8e7f6\n"
        "    exerciseName: Corsa leggera\n"
        "    type: CARDIO\n"
        "    bodypart: cardio\n"
        "    excludeFromTonnage: true\n"
        "    sets:\n"
        '      - startedAt: "2026-08-12T10:15:03"\n'
        '        endedAt: "2026-08-12T10:45:10"\n'
        "        avgHr: 138\n"
        "        maxHr: 156\n",
    ).encode()
    response = post_session(app_client, md, content_hash=content_hash_of(md))
    assert response.status_code == 200
    assert response.json()["status"] == "stored"

    conn = _db_conn(app_client)
    row = conn.execute(
        """SELECT exercise_type, reps, weight, started_at, ended_at, avg_hr, max_hr
           FROM exercise_sets WHERE session_id = ?""",
        ("3c4d5e6f",),
    ).fetchone()
    assert row == ("CARDIO", None, None, "2026-08-12T10:15:03", "2026-08-12T10:45:10", 138, 156)


def test_forza_only_session_leaves_cardio_columns_null(app_client) -> None:
    post_session(app_client)
    conn = _db_conn(app_client)
    rows = conn.execute(
        "SELECT started_at, ended_at, avg_hr, max_hr FROM exercise_sets WHERE session_id = ?",
        ("3c4d5e6f",),
    ).fetchall()
    assert rows == [(None, None, None, None), (None, None, None, None)]


def test_bodyweight_set_materialized_weight_and_audit_fields_round_trip(app_client) -> None:
    # The phone now writes a real positive `weight` for bodyweight sets plus
    # the bwLoadPercent/bwBaseWeightKg audit pair. weight must flow into
    # exercise_sets unchanged and land in tonnage like any other weight.
    md = SESSION_MD.replace(
        "    sets:\n"
        "      - reps: 8\n"
        "        weight: 80.0\n"
        "      - reps: 6\n"
        "        weight: 85.0\n",
        "    sets:\n"
        "      - reps: 10\n"
        "        weight: 61.5\n"
        "        isBodyweight: true\n"
        "        bwLoadPercent: 75\n"
        "        bwBaseWeightKg: 82.0\n",
    ).encode()
    response = post_session(app_client, md, content_hash=content_hash_of(md))
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        """SELECT reps, weight, is_bodyweight, bw_load_percent, bw_base_weight_kg
           FROM exercise_sets WHERE session_id = ?""",
        ("3c4d5e6f",),
    ).fetchone()
    assert row == (10, 61.5, 1, 75, 82.0)


def test_plain_weighted_session_leaves_bodyweight_load_columns_null(app_client) -> None:
    post_session(app_client)
    conn = _db_conn(app_client)
    rows = conn.execute(
        "SELECT bw_load_percent, bw_base_weight_kg FROM exercise_sets WHERE session_id = ?",
        ("3c4d5e6f",),
    ).fetchall()
    assert rows == [(None, None), (None, None)]


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


# ---------------------------------------------------------------------------
# Bulk sessions endpoint tests
# ---------------------------------------------------------------------------


def test_bulk_sessions_stored(app_client) -> None:
    """Two valid sessions in one bulk request — both stored, one commit."""
    md1 = SESSION_MD.replace("3c4d5e6f", "11111111").encode()
    md2 = SESSION_MD.replace("3c4d5e6f", "22222222").encode()
    sessions = [
        {
            "sessionId": "11111111",
            "relPath": "2025/04/sess_11111111.md",
            "contentHash": content_hash_of(md1),
            "appVersion": "1.0-test",
            "clientSentAt": "2025-04-15T10:05:42Z",
        },
        {
            "sessionId": "22222222",
            "relPath": "2025/04/sess_22222222.md",
            "contentHash": content_hash_of(md2),
            "appVersion": "1.0-test",
            "clientSentAt": "2025-04-15T10:05:42Z",
        },
    ]
    response = post_sessions_bulk(app_client, sessions, [md1, md2])
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 2
    assert body["results"][0] == {"sessionId": "11111111", "status": "stored"}
    assert body["results"][1] == {"sessionId": "22222222", "status": "stored"}

    # Verify both files exist in raw store
    import app.config
    assert (app.config.RAW_DIR / "2025/04/sess_11111111.md").exists()
    assert (app.config.RAW_DIR / "2025/04/sess_22222222.md").exists()

    # Verify both sessions in DB
    conn = _db_conn(app_client)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id IN (?, ?)",
            ("11111111", "22222222"),
        ).fetchone()[0]
        == 2
    )


def test_bulk_sessions_one_duplicate_one_stored(app_client) -> None:
    """First upload stores both; second bulk marks first as duplicate."""
    md1 = SESSION_MD.replace("3c4d5e6f", "aaaabbbb").encode()
    md2 = SESSION_MD.replace("3c4d5e6f", "cccceeee").encode()
    sessions = [
        {
            "sessionId": "aaaabbbb",
            "relPath": "2025/04/sess_aaaabbbb.md",
            "contentHash": content_hash_of(md1),
            "appVersion": "1.0-test",
            "clientSentAt": "2025-04-15T10:05:42Z",
        },
        {
            "sessionId": "cccceeee",
            "relPath": "2025/04/sess_cccceeee.md",
            "contentHash": content_hash_of(md2),
            "appVersion": "1.0-test",
            "clientSentAt": "2025-04-15T10:05:42Z",
        },
    ]
    # First bulk
    r1 = post_sessions_bulk(app_client, sessions, [md1, md2])
    assert r1.json()["results"] == [
        {"sessionId": "aaaabbbb", "status": "stored"},
        {"sessionId": "cccceeee", "status": "stored"},
    ]

    # Second bulk (same data)
    r2 = post_sessions_bulk(app_client, sessions, [md1, md2])
    assert r2.json()["results"] == [
        {"sessionId": "aaaabbbb", "status": "duplicate"},
        {"sessionId": "cccceeee", "status": "duplicate"},
    ]


def test_bulk_sessions_hash_mismatch_is_per_entry_error(app_client) -> None:
    """Hash mismatch on one entry doesn't fail the whole request — it's an
    error for that entry only, other entries still process."""
    md1 = SESSION_MD.replace("3c4d5e6f", "ffffffff").encode()
    md2 = SESSION_MD.replace("3c4d5e6f", "eeeeeeee").encode()
    sessions = [
        {
            "sessionId": "ffffffff",
            "relPath": "2025/04/sess_ffffffff.md",
            "contentHash": content_hash_of(md1),  # correct
            "appVersion": "1.0-test",
            "clientSentAt": "2025-04-15T10:05:42Z",
        },
        {
            "sessionId": "eeeeeeee",
            "relPath": "2025/04/sess_eeeeeeee.md",
            "contentHash": "sha256:" + "0" * 64,  # WRONG hash
            "appVersion": "1.0-test",
            "clientSentAt": "2025-04-15T10:05:42Z",
        },
    ]
    response = post_sessions_bulk(app_client, sessions, [md1, md2])
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 2
    # First entry: stored
    assert body["results"][0] == {"sessionId": "ffffffff", "status": "stored"}
    # Second entry: error (hash mismatch)
    assert body["results"][1]["sessionId"] == "eeeeeeee"
    assert body["results"][1]["status"] == "error"
    assert "contentHash mismatch" in body["results"][1]["error"]


def test_bulk_sessions_too_many_entries_returns_413(app_client) -> None:
    """Exceeding BULK_MAX_FILES returns 413."""
    from app.repo import BULK_MAX_FILES

    sessions = []
    files = []
    for i in range(BULK_MAX_FILES + 1):
        md = SESSION_MD.replace("3c4d5e6f", f"{i:08x}").encode()
        sessions.append(
            {
                "sessionId": f"{i:08x}",
                "relPath": f"2025/04/sess_{i:08x}.md",
                "contentHash": content_hash_of(md),
                "appVersion": "1.0-test",
                "clientSentAt": "2025-04-15T10:05:42Z",
            }
        )
        files.append(md)

    response = post_sessions_bulk(app_client, sessions, files)
    assert response.status_code == 413
    assert "too many entries" in response.json()["error"]


def test_bulk_sessions_total_bytes_exceeds_limit_returns_413(app_client) -> None:
    """Exceeding BULK_MAX_TOTAL_BYTES returns 413."""
    from app.repo import BULK_MAX_TOTAL_BYTES

    # One huge session that exceeds the limit
    huge_md = b"x" * (BULK_MAX_TOTAL_BYTES + 1)
    sessions = [
        {
            "sessionId": "huge1234",
            "relPath": "2025/04/huge.md",
            "contentHash": content_hash_of(huge_md),
            "appVersion": "1.0-test",
            "clientSentAt": "2025-04-15T10:05:42Z",
        }
    ]
    response = post_sessions_bulk(app_client, sessions, [huge_md])
    assert response.status_code == 413
    assert "exceeds" in response.json()["error"]
