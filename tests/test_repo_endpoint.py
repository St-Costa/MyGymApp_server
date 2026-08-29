"""docs/backup-server-brief.md Tasks 2 & 3: POST /v1/repo (upsert/delete,
soft-delete, path safety, the _-prefixed debug file) plus GET /v1/manifest
and GET /v1/file.

The git commit and the SQL-view parse both run off the request path now
(docs/backup/OPTIMIZATION.md points 1 & 6), so tests that assert on parsed
rows or on `git log` call settle_backup() first."""

import sqlite3

from tests.conftest import (
    EXERCISE_MD,
    ROUTINE_MD,
    auth_headers,
    content_hash_of,
    post_repo,
    post_session,
    settle_backup,
)


def _db(app_client) -> sqlite3.Connection:
    import app.config

    return sqlite3.connect(app.config.DB_PATH)


# --- upsert -----------------------------------------------------------------


def test_upsert_exercise_stores_file_and_row(app_client) -> None:
    resp = post_repo(app_client)
    assert resp.status_code == 200
    assert resp.json() == {
        "relPath": "exercises/bench-press-ex-a1b2c3d4.md",
        "status": "stored",
    }

    import app.config

    assert (app.config.RAW_DIR / "exercises/bench-press-ex-a1b2c3d4.md").exists()

    settle_backup()
    row = (
        _db(app_client)
        .execute(
            "SELECT id, name, type, bodypart, default_rep_min, default_rep_max, "
            "is_bodyweight, deleted_at FROM exercises WHERE id = ?",
            ("ex-a1b2c3d4",),
        )
        .fetchone()
    )
    assert row == ("ex-a1b2c3d4", "Bench Press", "FORZA", "chest", 8, 12, 0, None)


def test_upsert_routine_stores_child_rows(app_client) -> None:
    resp = post_repo(
        app_client,
        ROUTINE_MD.encode(),
        rel_path="routines/push-day-rt-b2c3d4e5.md",
    )
    assert resp.json()["status"] == "stored"
    settle_backup()

    conn = _db(app_client)
    routine_row = conn.execute(
        "SELECT name, day, enabled FROM routines WHERE id = ?", ("rt-b2c3d4e5",)
    ).fetchone()
    assert routine_row == ("Push Day", "monday", 1)
    kids = conn.execute(
        "SELECT position, exercise_id, sets, rep_min, rep_max, time_per_set_seconds, "
        "superset_with_next, is_warmup FROM routine_exercises "
        "WHERE routine_id = ? ORDER BY position",
        ("rt-b2c3d4e5",),
    ).fetchall()
    assert kids == [
        (0, "ex-a1b2c3d4", 4, 6, 10, 60, 0, 1),
        (1, "ex-b2c3d4e5", 3, 8, 12, None, 0, 0),
    ]


def test_identical_reupsert_is_duplicate_no_commit(app_client) -> None:
    assert post_repo(app_client).json()["status"] == "stored"

    second = post_repo(app_client)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


def test_upsert_changed_bytes_overwrites_and_reparses(app_client) -> None:
    assert post_repo(app_client).json()["status"] == "stored"

    edited = EXERCISE_MD.replace("bodypart: chest", "bodypart: shoulders").encode()
    resp = post_repo(app_client, edited, content_hash=content_hash_of(edited))
    assert resp.json()["status"] == "stored"
    settle_backup()

    row = (
        _db(app_client)
        .execute("SELECT bodypart FROM exercises WHERE id = ?", ("ex-a1b2c3d4",))
        .fetchone()
    )
    assert row == ("shoulders",)


def test_upsert_hash_mismatch_writes_nothing(app_client) -> None:
    resp = post_repo(app_client, content_hash="sha256:" + "0" * 64)
    assert resp.status_code == 422

    import app.config

    assert not (app.config.RAW_DIR / "exercises/bench-press-ex-a1b2c3d4.md").exists()
    assert _db(app_client).execute("SELECT COUNT(*) FROM exercises").fetchone()[0] == 0


def test_upsert_missing_required_envelope_field_is_422(app_client) -> None:
    assert post_repo(app_client, omit_field="op").status_code == 422
    assert post_repo(app_client, omit_field="relPath").status_code == 422


def test_unknown_op_is_422(app_client) -> None:
    assert post_repo(app_client, op="frobnicate").status_code == 422


# --- path safety (§2.1) ---------------------------------------------------


def test_path_traversal_is_rejected(app_client) -> None:
    for bad in (
        "../../etc/passwd",
        "/etc/passwd",
        "exercises/../../secret.md",
        "sessions/2026/08/foo.md",  # outside exercises/ | routines/
    ):
        resp = post_repo(app_client, rel_path=bad)
        assert resp.status_code == 422, bad

    import app.config

    assert not (app.config.RAW_DIR / "secret.md").exists()


# --- delete (§2.3) ------------------------------------------------------------


def test_delete_soft_deletes_to_tombstone(app_client) -> None:
    assert post_repo(app_client).json()["status"] == "stored"
    settle_backup()

    resp = post_repo(app_client, md_bytes=None, op="delete")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"

    import app.config

    live = app.config.RAW_DIR / "exercises/bench-press-ex-a1b2c3d4.md"
    assert not live.exists()
    tombstone_dir = app.config.RAW_DIR / "deleted/exercises"
    tombstones = list(tombstone_dir.glob("bench-press-ex-a1b2c3d4.md.*"))
    assert len(tombstones) == 1

    settle_backup()
    # Row is marked deleted, not dropped.
    row = (
        _db(app_client)
        .execute("SELECT deleted_at FROM exercises WHERE id = ?", ("ex-a1b2c3d4",))
        .fetchone()
    )
    assert row is not None and row[0] is not None


def test_delete_of_absent_file_is_already_absent_no_commit(app_client) -> None:
    resp = post_repo(
        app_client, md_bytes=None, op="delete", rel_path="exercises/nope-ex-00000000.md"
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_absent"


def test_reupsert_after_delete_revives_row(app_client) -> None:
    assert post_repo(app_client).json()["status"] == "stored"
    settle_backup()
    assert post_repo(app_client, md_bytes=None, op="delete").json()["status"] == "deleted"
    settle_backup()

    assert post_repo(app_client).json()["status"] == "stored"
    settle_backup()
    row = (
        _db(app_client)
        .execute("SELECT deleted_at FROM exercises WHERE id = ?", ("ex-a1b2c3d4",))
        .fetchone()
    )
    assert row == (None,)


# --- _-prefixed debug file (§2.1 / §4) --------------------------------------


def test_debug_backup_file_is_committed_but_never_a_row(app_client) -> None:
    debug_md = b"---\nid: rt-deadbeef\nname: debug\ndebug: true\n---\n"
    rel = "routines/_debug-backup-1725480000000.md"

    up = post_repo(app_client, debug_md, rel_path=rel, content_hash=content_hash_of(debug_md))
    assert up.json()["status"] == "stored"
    settle_backup()

    import app.config

    assert (app.config.RAW_DIR / rel).exists()
    # No routines row for the _-prefixed file.
    assert _db(app_client).execute("SELECT COUNT(*) FROM routines").fetchone()[0] == 0

    dele = post_repo(app_client, md_bytes=None, op="delete", rel_path=rel)
    assert dele.json()["status"] == "deleted"
    settle_backup()
    tombstones = list((app.config.RAW_DIR / "deleted/routines").glob("_debug-backup-*"))
    assert len(tombstones) == 1


# --- auth -----------------------------------------------------------------


def test_repo_requires_bearer_token(app_client) -> None:
    assert post_repo(app_client, headers={}).status_code == 401
    assert post_repo(app_client, headers={"Authorization": "Bearer wrong"}).status_code == 401


# --- manifest + file (Task 3) ------------------------------------------------


def test_manifest_lists_live_files_not_deleted(app_client) -> None:
    assert post_session(app_client).status_code == 200
    assert post_repo(app_client).json()["status"] == "stored"
    assert post_repo(app_client, md_bytes=None, op="delete").json()["status"] == "deleted"

    manifest = app_client.get("/v1/manifest", headers=auth_headers()).json()
    files = manifest["files"]

    assert any(k.endswith("_rt-b2c3d4e5_3c4d5e6f.md") for k in files)  # the session
    # The deleted exercise must not appear.
    assert "exercises/bench-press-ex-a1b2c3d4.md" not in files
    assert all(not k.startswith("deleted/") for k in files)
    assert "generatedAt" in manifest


def test_get_file_returns_exact_bytes_and_hash(app_client) -> None:
    assert post_repo(app_client).json()["status"] == "stored"

    rel = "exercises/bench-press-ex-a1b2c3d4.md"
    resp = app_client.get("/v1/file", params={"relPath": rel}, headers=auth_headers())
    assert resp.status_code == 200
    assert resp.content == EXERCISE_MD.encode()
    assert resp.headers["X-Content-SHA256"] == content_hash_of(EXERCISE_MD.encode())


def test_get_file_traversal_is_422(app_client) -> None:
    resp = app_client.get(
        "/v1/file", params={"relPath": "../../etc/passwd"}, headers=auth_headers()
    )
    assert resp.status_code == 422


def test_get_file_missing_is_404(app_client) -> None:
    resp = app_client.get(
        "/v1/file", params={"relPath": "exercises/nope-ex-00000000.md"}, headers=auth_headers()
    )
    assert resp.status_code == 404


def test_get_file_under_deleted_is_404(app_client) -> None:
    assert post_repo(app_client).json()["status"] == "stored"
    assert post_repo(app_client, md_bytes=None, op="delete").json()["status"] == "deleted"

    import app.config

    tombstone = next((app.config.RAW_DIR / "deleted/exercises").glob("*"))
    rel = tombstone.relative_to(app.config.RAW_DIR).as_posix()
    resp = app_client.get("/v1/file", params={"relPath": rel}, headers=auth_headers())
    assert resp.status_code == 404
