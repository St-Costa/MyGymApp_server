"""docs/backup-server-brief.md Task 1: data/raw/ is a git repository, one
commit per successful ingest."""

import subprocess

from tests.conftest import (
    SESSION_MD,
    content_hash_of,
    post_repo,
    post_session,
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
    # Fresh tmp DATA_ROOT has an empty raw/ — init makes the repo but the
    # first commit only lands on the first real ingest.
    name = _git(app_client, "config", "user.name").strip()
    assert name == "MyGymApp Sync"


def test_session_push_makes_one_commit(app_client) -> None:
    assert post_session(app_client).status_code == 200

    subjects = _log_subjects(app_client)
    assert len(subjects) == 1
    assert subjects[0].startswith("sync ")
    assert "sessions:1 readiness:0 scale:0 ecg:0 repo:0" in subjects[0]

    # The raw file is tracked and clean (committed, not just written).
    status = _git(app_client, "status", "--porcelain")
    assert status.strip() == ""


def test_duplicate_delivery_makes_no_new_commit(app_client) -> None:
    assert post_session(app_client).status_code == 200
    before = _log_subjects(app_client)

    dup = post_session(app_client)
    assert dup.json()["status"] == "duplicate"
    assert _log_subjects(app_client) == before  # no second commit


def test_session_edit_makes_a_second_commit(app_client) -> None:
    assert post_session(app_client).status_code == 200

    edited = SESSION_MD.replace("totalTonnage: 12450.0", "totalTonnage: 13000.0").encode()
    resp = post_session(app_client, edited, content_hash=content_hash_of(edited))
    assert resp.status_code == 200

    assert len(_log_subjects(app_client)) == 2


def test_db_is_not_committed(app_client) -> None:
    """The SQLite DB lives at data/app.db-style path *outside* data/raw/ —
    here it's data/db.sqlite3 — so it can never be swept into a commit."""
    assert post_session(app_client).status_code == 200
    tracked = _git(app_client, "ls-files")
    assert "db.sqlite3" not in tracked
    assert ".sqlite3" not in tracked


def test_repo_push_commit_message_counts_repo(app_client) -> None:
    assert post_repo(app_client).status_code == 200
    subjects = _log_subjects(app_client)
    assert subjects[0].startswith("sync ")
    assert "repo:1" in subjects[0]
