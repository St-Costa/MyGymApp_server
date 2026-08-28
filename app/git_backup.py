"""`data/raw/` as a git repository — one commit per successful ingest.

Task 1 of docs/backup-server-brief.md. The raw store is already the durable
source of truth (see docs/sync-ingestion/SPEC.md "raw-first, parse-second");
wrapping it in git makes every prior state — and every accidentally deleted
file — recoverable, and gives `POST /v1/repo` (app/repo.py) a natural
soft-delete + history model to build on.

Design points held to here:

- **One commit per request.** `commit_raw_store()` is called once, at the
  end of a successful ingest, after the raw file(s) are durably on disk. It
  stages everything under `data/raw/` and commits iff something is staged
  (an idempotent re-push of identical bytes stages nothing -> no commit).
- **Process-wide lock.** All staging/committing goes through `_LOCK` so two
  concurrent ingests can't corrupt the index. The ingest handlers are
  `async def` but do their work synchronously (see app/routes.py), so a
  plain `threading.Lock` is enough and matches the rest of the code.
- **Never fatal to the raw write.** Init failure or commit failure is logged
  loudly; the caller decides whether that's a 500 (it is — see app/routes.py).
  The raw bytes are already on disk regardless, and the next successful
  push's `git add -A` sweeps in anything a failed commit left uncommitted.
- **No gc/repack cron.** These are tiny text files; git's default automatic
  gc is fine. See the brief.
"""

import logging
import subprocess
import threading
from datetime import UTC, datetime

from app.config import RAW_DIR

logger = logging.getLogger("mygymapp.git_backup")

# Serializes every git index operation on data/raw/ (init, add, commit,
# push). Module-global on purpose: one lock per process, matching the
# single-process deployment (uvicorn, one worker — see the Dockerfile).
_LOCK = threading.Lock()

_GIT_USER_NAME = "MyGymApp Sync"
_GIT_USER_EMAIL = "sync@localhost"


class GitBackupError(RuntimeError):
    """A git operation on the raw store failed. Raised by commit_raw_store()
    so the route layer can turn it into a 500 — the raw file is still on
    disk, but this request didn't cross the durability boundary a commit
    represents (see the brief, Task 1.2)."""


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """`git -C <RAW_DIR> <args>`, capturing output. `check=True` raises
    CalledProcessError on a non-zero exit (callers translate that)."""
    return subprocess.run(
        ["git", "-C", str(RAW_DIR), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def ensure_repo() -> None:
    """Task 1.1. Idempotent: if `data/raw/.git` already exists, does nothing.
    Otherwise `git init` + baseline config + an "import existing store"
    commit picking up whatever files are already there (~130 weigh-ins, ~60
    sessions, readiness, ecg on a real deployment).

    Called once at startup (app/main.py's lifespan). Any failure here is
    logged and re-raised — a server that can't maintain its backup repo
    should fail startup loudly rather than silently stop versioning.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if (RAW_DIR / ".git").exists():
        return

    with _LOCK:
        # Re-check under the lock in case two startup paths race (they don't
        # today, but this keeps ensure_repo() honestly idempotent).
        if (RAW_DIR / ".git").exists():
            return
        try:
            _run_git("init")
            _run_git("config", "user.name", _GIT_USER_NAME)
            _run_git("config", "user.email", _GIT_USER_EMAIL)
            # No signing key on a headless box; an unsigned commit is fine
            # for a local-only backup repo.
            _run_git("config", "commit.gpgsign", "false")

            _run_git("add", "-A")
            # A brand-new DATA_ROOT has an empty raw/ — nothing to commit
            # yet. Don't fail; the first real ingest makes the first commit.
            if _has_staged_changes():
                _run_git("commit", "-m", "import existing store")
        except subprocess.CalledProcessError as exc:
            logger.error("failed to initialize git repo at %s: %s", RAW_DIR, exc.stderr.strip())
            raise GitBackupError(f"git init failed: {exc.stderr.strip()}") from exc

    logger.info("initialized backup git repo at %s", RAW_DIR)


def _has_staged_changes() -> bool:
    """True iff `git add -A` staged anything — i.e. a commit would be
    non-empty. `git diff --cached --quiet` exits 1 when there are staged
    changes, 0 when the index matches HEAD."""
    return _run_git("diff", "--cached", "--quiet", check=False).returncode != 0


def format_commit_message(
    *,
    sessions: int = 0,
    readiness: int = 0,
    scale: int = 0,
    ecg: int = 0,
    repo: int = 0,
    note: str | None = None,
) -> str:
    """The brief's commit-message format:

        sync 2026-09-04T20:11:03Z — sessions:1 readiness:0 scale:0 ecg:0 repo:2

    i.e. `sync <ISO8601 UTC> — ` then a per-record-type count of files
    touched *in this request*. `note` appends a parenthetical (used by
    /v1/repo's delete path to record the tombstone move)."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts = f"sessions:{sessions} readiness:{readiness} scale:{scale} ecg:{ecg} repo:{repo}"
    message = f"sync {stamp} — {counts}"
    if note:
        message += f" ({note})"
    return message


def commit_raw_store(message: str) -> bool:
    """Stage everything under `data/raw/` and commit iff something is staged.

    Returns True if a commit was made, False if there was nothing to commit
    (an idempotent re-push of identical bytes — the caller has already
    handled the "duplicate" response by the time it gets here, but a
    to-the-byte-identical write can still stage nothing).

    Raises GitBackupError on an actual git failure (disk full mid-commit,
    corrupt index, …) — the route turns that into a 500. The raw file is
    already on disk; the next successful push's `git add -A` sweeps it in.

    Also pushes to `BACKUP_GIT_REMOTE` if set (Task 1.3) — best-effort,
    a push failure is logged, never fatal.
    """
    with _LOCK:
        try:
            _run_git("add", "-A")
            if not _has_staged_changes():
                return False
            _run_git("commit", "-m", message)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "git commit failed for raw store (%s): %s",
                message,
                exc.stderr.strip(),
            )
            raise GitBackupError(f"git commit failed: {exc.stderr.strip()}") from exc

        _maybe_push()
        return True


def git_mv(src_rel: str, dest_rel: str) -> None:
    """`git -C data/raw mv <src> <dest>`, creating the destination's parent
    dirs first (git mv won't). Used by /v1/repo's soft-delete to move a file
    into `deleted/<relPath>.<unix_ts>` — see app/repo.py. Staging only; the
    caller commits afterwards via commit_raw_store()."""
    with _LOCK:
        dest_abs = RAW_DIR / dest_rel
        dest_abs.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run_git("mv", src_rel, dest_rel)
        except subprocess.CalledProcessError as exc:
            logger.error("git mv %s -> %s failed: %s", src_rel, dest_rel, exc.stderr.strip())
            raise GitBackupError(f"git mv failed: {exc.stderr.strip()}") from exc


def _maybe_push() -> None:
    """Task 1.3: optional off-box mirror. If `BACKUP_GIT_REMOTE` is set, push
    HEAD to it once, best-effort. A push failure is logged and swallowed —
    the local commit is the real backup, the mirror is redundancy. Never
    retried inline. Read through app.config at call time so a test or a
    restart can change it without this module caching a stale value."""
    from app import config

    remote = config.BACKUP_GIT_REMOTE
    if not remote:
        return
    result = _run_git("push", "--quiet", remote, "HEAD:main", check=False)
    if result.returncode != 0:
        logger.warning(
            "backup mirror push to %s failed (local commit is safe): %s",
            remote,
            result.stderr.strip(),
        )
