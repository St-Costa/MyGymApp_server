"""docs/backup-server-brief.md Task 1 + docs/backup/OPTIMIZATION.md point 1:
data/raw/ is a git repository, and commits are debounced into one per burst
rather than one per request."""

import subprocess

from tests.conftest import (
    SESSION_MD,
    content_hash_of,
    post_repo,
    post_session,
    settle_backup,
)


def _git(app_client, *args: str) -> str:
    import app.config

    return subprocess.run(
        ["git", "-C", str(app.config.RAW_DIR), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _log_subjects(app_client) -> list[str]:
    out = _git(app_client, "log", "--format=%s")
    return [line for line in out.splitlines() if line]


def test_startup_initializes_repo(app_client) -> None:
    import app.config

    assert (app.config.RAW_DIR / ".git").is_dir()
    name = _git(app_client, "config", "user.name").strip()
    assert name == "MyGymApp Sync"


def test_session_push_makes_one_commit(app_client) -> None:
    assert post_session(app_client).status_code == 200
    settle_backup()

    subjects = _log_subjects(app_client)
    assert len(subjects) == 1
    assert subjects[0].startswith("sync ")
    assert "sessions:1 readiness:0 scale:0 ecg:0 repo:0" in subjects[0]

    # The raw file is tracked and clean (committed, not just written).
    status = _git(app_client, "status", "--porcelain")
    assert status.strip() == ""


def test_burst_of_writes_coalesces_into_one_commit(app_client) -> None:
    """docs/backup/OPTIMIZATION.md point 1: N writes with no quiet gap
    between them become a single commit whose message sums the burst."""
    for i in range(5):
        md = SESSION_MD.replace("id: 3c4d5e6f", f"id: sess{i:04d}").encode()
        rel = f"2025/04/2025-04-15_rt-b2c3d4e5_sess{i:04d}.md"
        resp = post_session(
            app_client,
            md,
            session_id=f"sess{i:04d}",
            rel_path=rel,
            content_hash=content_hash_of(md),
        )
        assert resp.status_code == 200
    settle_backup()

    subjects = _log_subjects(app_client)
    assert len(subjects) == 1
    assert "sessions:5" in subjects[0]


def test_duplicate_delivery_makes_no_new_commit(app_client) -> None:
    assert post_session(app_client).status_code == 200
    settle_backup()
    before = _log_subjects(app_client)

    dup = post_session(app_client)
    assert dup.json()["status"] == "duplicate"
    settle_backup()
    assert _log_subjects(app_client) == before  # no second commit


def test_session_edit_makes_a_second_commit(app_client) -> None:
    assert post_session(app_client).status_code == 200
    settle_backup()

    edited = SESSION_MD.replace("totalTonnage: 12450.0", "totalTonnage: 13000.0").encode()
    resp = post_session(app_client, edited, content_hash=content_hash_of(edited))
    assert resp.status_code == 200
    settle_backup()

    assert len(_log_subjects(app_client)) == 2


def test_db_is_not_committed(app_client) -> None:
    assert post_session(app_client).status_code == 200
    settle_backup()
    tracked = _git(app_client, "ls-files")
    assert "db.sqlite3" not in tracked
    assert ".sqlite3" not in tracked


def test_repo_push_commit_message_counts_repo(app_client) -> None:
    assert post_repo(app_client).status_code == 200
    settle_backup()
    subjects = _log_subjects(app_client)
    assert subjects[0].startswith("sync ")
    assert "repo:1" in subjects[0]


def test_shutdown_flush_commits_pending_burst(app_client) -> None:
    """A clean shutdown flushes whatever the debounce timer hadn't yet
    committed, so a restart never inherits uncommitted raw writes. The
    app_client fixture's `with TestClient(...)` runs the lifespan;
    settle_backup() here stands in for that flush and must leave the tree
    clean."""
    assert post_session(app_client).status_code == 200
    settle_backup()
    assert _git(app_client, "status", "--porcelain").strip() == ""
