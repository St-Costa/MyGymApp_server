"""docs/backup/OPTIMIZATION.md points 4 & 6: manifest caching and the
off-request-path repo-file parse worker. (Point 1's debounce coalescing is
covered in test_git_backup.py; point 5's os.rename soft-delete in
test_repo_endpoint.py.)"""

import sqlite3
from unittest.mock import patch

from tests.conftest import auth_headers, content_hash_of, post_repo, settle_backup


def _db(app_client) -> sqlite3.Connection:
    import app.config

    return sqlite3.connect(app.config.DB_PATH)


# --- point 4: manifest cache ------------------------------------------------


def test_manifest_is_cached_between_calls_with_no_writes(app_client) -> None:
    assert post_repo(app_client).json()["status"] == "stored"
    settle_backup()

    import app.repo

    first = app_client.get("/v1/manifest", headers=auth_headers()).json()

    # A second call with nothing changed must not re-walk / re-hash the tree.
    with patch.object(
        app.repo, "_compute_manifest_files", wraps=app.repo._compute_manifest_files
    ) as spy:
        second = app_client.get("/v1/manifest", headers=auth_headers()).json()
    spy.assert_not_called()

    assert first["files"] == second["files"]
    # generatedAt is still refreshed each call.
    assert "generatedAt" in second


def test_manifest_cache_invalidated_by_a_new_write(app_client) -> None:
    assert post_repo(app_client).json()["status"] == "stored"
    settle_backup()
    before = app_client.get("/v1/manifest", headers=auth_headers()).json()["files"]
    assert "exercises/bench-press-ex-a1b2c3d4.md" in before

    other = b"---\nid: ex-99999999\nname: Squat\ntype: forza\n---\n"
    assert (
        post_repo(
            app_client,
            other,
            rel_path="exercises/squat-ex-99999999.md",
            content_hash=content_hash_of(other),
        ).json()["status"]
        == "stored"
    )
    settle_backup()

    after = app_client.get("/v1/manifest", headers=auth_headers()).json()["files"]
    assert "exercises/squat-ex-99999999.md" in after
    assert len(after) == len(before) + 1


# --- point 6: parse worker ------------------------------------------------


def test_upsert_returns_before_parse_then_row_appears(app_client) -> None:
    """The 2xx comes back on the fsync; the exercises row shows up once the
    worker drains (settle_backup)."""
    resp = post_repo(app_client)
    assert resp.status_code == 200

    settle_backup()
    row = (
        _db(app_client)
        .execute("SELECT name FROM exercises WHERE id = ?", ("ex-a1b2c3d4",))
        .fetchone()
    )
    assert row == ("Bench Press",)


def test_malformed_repo_file_still_2xx_and_recorded_as_parse_failure(app_client) -> None:
    """A repo file the parser can't make sense of must not fail the request;
    the raw file is stored and the failure lands in parse_failures for
    reparse.py to retry."""
    bad = b"---\nnot: valid\nno_id_here: true\n---\nbody\n"
    resp = post_repo(
        app_client,
        bad,
        rel_path="exercises/broken-ex-deadbeef.md",
        content_hash=content_hash_of(bad),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "stored"
    settle_backup()

    import app.config

    assert (app.config.RAW_DIR / "exercises/broken-ex-deadbeef.md").exists()
    failures = (
        _db(app_client)
        .execute("SELECT COUNT(*) FROM parse_failures WHERE record_type = 'exercises'")
        .fetchone()[0]
    )
    assert failures == 1
