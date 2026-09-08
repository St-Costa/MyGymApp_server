"""`data/raw/` as a git repository — commits are *debounced*, not one per
request.

Originally (docs/backup-server-brief.md Task 1) this did one
`git add -A && git commit` inside each ingest handler. That made every bulk
operation scale with N: 50 files synced = 50 `git` forks, 50 tree writes,
all on the request path. docs/backup/OPTIMIZATION.md point 1 replaces that
with a background committer:

- Handlers call `request_commit(...)` after the raw bytes are on disk and
  hash-verified. It never blocks and never raises — it accumulates the
  per-record-type counts for this burst and (re)arms a quiet-period timer.
- A daemon thread (`_committer_loop`) waits until `COMMIT_DEBOUNCE_SECONDS`
  of write silence, then does a single `git add -A && git commit` covering
  the whole burst.
- `flush_pending_commit()` forces that commit immediately — called on
  clean shutdown (app/main.py lifespan) and by the tests.

Durability contract change (see docs/backup/README.md): a 2xx from an
ingest endpoint now means "raw bytes fsynced + hash verified", NOT
"committed". A crash between the fsync and the debounced commit leaves the
files uncommitted on the working tree; the next `git add -A` — the next
burst's commit, or `ensure_repo`/`flush` on restart — sweeps them in.
Nothing is lost because the raw files are the source of truth, exactly as
before.

Still true: one process, one worker (see the Dockerfile); `_LOCK`
serializes every git index operation; no `git gc`/`repack` on any path.
"""

import logging
import os
import subprocess
import threading
import time
from datetime import UTC, datetime

from app.config import RAW_DIR

logger = logging.getLogger("mygymapp.git_backup")

# Serializes every git index operation on data/raw/ (init, add, commit,
# push). Held only by ensure_repo() and the committer thread now that
# handlers no longer commit inline.
_LOCK = threading.Lock()

_GIT_USER_NAME = "MyGymApp Sync"
_GIT_USER_EMAIL = "sync@localhost"

# Author/committer identity passed as env on every commit, so `git commit`
# never has to read repo/user/system config to resolve it (config is still
# set once in ensure_repo() as a belt-and-braces fallback for anyone
# running git by hand in the repo). See docs/backup/OPTIMIZATION.md point 1.
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": _GIT_USER_NAME,
    "GIT_AUTHOR_EMAIL": _GIT_USER_EMAIL,
    "GIT_COMMITTER_NAME": _GIT_USER_NAME,
    "GIT_COMMITTER_EMAIL": _GIT_USER_EMAIL,
    "GIT_CONFIG_GLOBAL": os.devnull,  # don't read ~/.gitconfig on every commit
    "GIT_CONFIG_SYSTEM": os.devnull,
}

# How long the raw store must be write-quiet before the debounced committer
# flushes the accumulated burst as one commit. ~3s: long enough that a
# backfill of dozens of files coalesces into a single commit, short enough
# that a lone fine-of-session push is versioned almost immediately.
COMMIT_DEBOUNCE_SECONDS = 3.0


class GitBackupError(RuntimeError):
    """A git operation on the raw store failed. Raised only by the
    synchronous paths that still exist — ensure_repo() (startup) and
    flush_pending_commit() when called with raise_on_error. The debounced
    committer never raises into a request; it logs loudly and lets the next
    `git add -A` recover (the raw files are already safe on disk)."""


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """`git -C <RAW_DIR> <args>` with the preset identity env, capturing
    output. `check=True` raises CalledProcessError on a non-zero exit."""
    return subprocess.run(
        ["git", "-C", str(RAW_DIR), *args],
        check=check,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def ensure_repo() -> None:
    """Task 1.1. Idempotent: if `data/raw/.git` already exists, does nothing
    but sweep in any files a prior run left uncommitted (a crash between an
    fsync and the debounced commit). Otherwise `git init` + baseline config
    + an "import existing store" commit.

    Called once at startup (app/main.py's lifespan). A failure here is
    logged and re-raised — a server that can't maintain its backup repo
    should fail startup loudly.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    with _LOCK:
        fresh = not (RAW_DIR / ".git").exists()
        try:
            if fresh:
                _run_git("init")
                _run_git("config", "user.name", _GIT_USER_NAME)
                _run_git("config", "user.email", _GIT_USER_EMAIL)
                _run_git("config", "commit.gpgsign", "false")

            _run_git("add", "-A")
            if _has_staged_changes():
                # Fresh repo -> "import existing store". Existing repo with
                # staged changes -> a previous run's debounced commit never
                # landed; sweep it in now rather than losing the burst's
                # commit boundary silently.
                msg = "import existing store" if fresh else _sweep_message()
                _commit(msg)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "failed to initialize/sweep git repo at %s: %s", RAW_DIR, exc.stderr.strip()
            )
            raise GitBackupError(f"git init failed: {exc.stderr.strip()}") from exc

    if fresh:
        logger.info("initialized backup git repo at %s", RAW_DIR)


def _has_staged_changes() -> bool:
    """True iff `git add -A` staged anything — i.e. a commit would be
    non-empty. `git diff --cached --quiet` exits 1 when there are staged
    changes, 0 when the index matches HEAD."""
    return _run_git("diff", "--cached", "--quiet", check=False).returncode != 0


def _commit(message: str) -> None:
    """`git commit` with the flags that keep it off the config/hook/gpg
    slow paths (docs/backup/OPTIMIZATION.md point 1). Identity comes from
    `_GIT_ENV`, not `git config`, so no per-commit config read."""
    _run_git("commit", "--no-verify", "--no-gpg-sign", "--quiet", "-m", message)


def _sweep_message() -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"sync {stamp} — sweep uncommitted raw writes"


# ---------------------------------------------------------------------------
# Debounced committer
# ---------------------------------------------------------------------------

# Guards the pending-burst state below.
_pending_lock = threading.Lock()
# Accumulated per-record-type counts for the burst not yet committed.
_pending_counts: dict[str, int] = {}
# Free-text notes (e.g. delete tombstone moves) to append to the message.
_pending_notes: list[str] = []
# Monotonic timestamp of the most recent request_commit() call; the
# committer flushes once `now - _last_request >= COMMIT_DEBOUNCE_SECONDS`.
_last_request: float | None = None
# Set by flush_pending_commit() to bypass the debounce wait once; the
# committer clears it and sets `_flush_done` when it has finished the
# flush commit (or found nothing to commit).
_flush_now = threading.Event()
_flush_done = threading.Event()
# Set at shutdown to stop the committer thread after a final flush.
_stop = threading.Event()
# Wakes the committer loop when new work arrives or a flush is requested.
_wake = threading.Event()
_committer_thread: threading.Thread | None = None

_COUNT_KEYS = ("sessions", "readiness", "scale", "ecg", "repo")


def request_commit(*, note: str | None = None, **counts: int) -> None:
    """Record that this request wrote to the raw store; the debounced
    committer will fold it into the next burst commit. Never blocks, never
    raises — the raw bytes are already fsynced by the time this is called,
    and a commit failure is the committer's problem to log, not this
    request's to surface. `counts` is any of sessions/readiness/scale/ecg/
    repo -> number of files that record type touched in this request."""
    for key in counts:
        if key not in _COUNT_KEYS:
            raise ValueError(f"unknown record-type count: {key}")
    with _pending_lock:
        for key, n in counts.items():
            _pending_counts[key] = _pending_counts.get(key, 0) + n
        if note:
            _pending_notes.append(note)
        global _last_request
        _last_request = time.monotonic()
    _wake.set()


def _burst_message() -> str:
    """Build the commit message for the accumulated burst and clear it.
    Caller holds nothing — this takes `_pending_lock` itself."""
    with _pending_lock:
        counts = {k: _pending_counts.get(k, 0) for k in _COUNT_KEYS}
        notes = list(_pending_notes)
        _pending_counts.clear()
        _pending_notes.clear()
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = " ".join(f"{k}:{counts[k]}" for k in _COUNT_KEYS)
    message = f"sync {stamp} — {body}"
    if notes:
        message += " (" + "; ".join(notes) + ")"
    return message


def _has_pending() -> bool:
    with _pending_lock:
        return bool(_pending_counts) or bool(_pending_notes)


def _do_commit_locked() -> None:
    """Stage + commit whatever is on the working tree, using the burst
    message. Holds `_LOCK`. Swallows nothing structurally, but a git failure
    is logged (not raised) — the files stay on the working tree for the next
    sweep. Also fires the optional off-box mirror push."""
    message = _burst_message()
    try:
        with _LOCK:
            _run_git("add", "-A")
            if not _has_staged_changes():
                return
            _commit(message)
            _maybe_push()
    except subprocess.CalledProcessError as exc:
        logger.error(
            "debounced git commit failed (%s): %s — files remain on the working "
            "tree, the next commit's `git add -A` will sweep them in",
            message,
            exc.stderr.strip(),
        )


def _committer_loop() -> None:
    """Daemon loop: wait for write activity, then flush one commit per
    quiet-period. Wakes on `_wake` (a request_commit, a flush, or a stop),
    never busy-polls. Runs until `_stop` is set, doing a final flush on the
    way out."""
    while not _stop.is_set():
        # Block until something happens. When a burst is mid-debounce, wait
        # only until the quiet period would elapse so we can commit on time.
        with _pending_lock:
            last = _last_request
            pending = bool(_pending_counts) or bool(_pending_notes)
        if pending and last is not None:
            wait_for = COMMIT_DEBOUNCE_SECONDS - (time.monotonic() - last)
            if wait_for > 0:
                # Debounce still running — wake early only if a flush/stop or
                # another request arrives; otherwise commit when it elapses.
                _wake.wait(timeout=wait_for)
            # else: quiet period already over, fall straight through to commit.
        else:
            _wake.wait()
        _wake.clear()

        if _stop.is_set():
            break

        if _flush_now.is_set():
            _flush_now.clear()
            if _has_pending():
                _do_commit_locked()
            _flush_done.set()  # signal flush_pending_commit() we're done
            continue

        with _pending_lock:
            last = _last_request
            pending = bool(_pending_counts) or bool(_pending_notes)
        if not pending:
            continue

        if last is not None and (time.monotonic() - last) >= COMMIT_DEBOUNCE_SECONDS:
            _do_commit_locked()
        # else: woke early (a new request_commit reset _last_request) —
        # loop, recompute the wait, and try again.

    # Shutdown: one last flush so a clean restart leaves nothing uncommitted.
    if _has_pending():
        _do_commit_locked()


def start_committer() -> None:
    """Start the background committer thread. Idempotent. Called from the
    app lifespan (app/main.py) after ensure_repo()."""
    global _committer_thread
    if _committer_thread is not None and _committer_thread.is_alive():
        return
    _stop.clear()
    _committer_thread = threading.Thread(
        target=_committer_loop, name="git-committer", daemon=True
    )
    _committer_thread.start()
    logger.info("started debounced git committer (debounce=%.1fs)", COMMIT_DEBOUNCE_SECONDS)


def flush_pending_commit(*, timeout: float = 10.0) -> None:
    """Force the debounced committer to commit any pending burst right now
    and wait (up to `timeout`) for it to actually finish the commit. Called
    on shutdown and by the tests. If the committer thread isn't running,
    commits inline."""
    if _committer_thread is None or not _committer_thread.is_alive():
        if _has_pending():
            _do_commit_locked()
        return
    _flush_done.clear()
    _flush_now.set()
    _wake.set()
    if not _flush_done.wait(timeout=timeout):
        logger.warning(
            "flush_pending_commit timed out after %.1fs waiting for the committer", timeout
        )


def stop_committer(*, timeout: float = 10.0) -> None:
    """Stop the committer thread after a final flush. Called on app
    shutdown (app/main.py lifespan)."""
    _stop.set()
    _wake.set()
    thread = _committer_thread
    if thread is not None:
        thread.join(timeout=timeout)


def _maybe_push() -> None:
    """docs/backup-server-brief.md Task 1.3: optional off-box mirror. If
    `BACKUP_GIT_REMOTE` is set, push HEAD to it once, best-effort — a push
    failure is logged and swallowed, the local commit is the real backup.
    Never retried inline. Read through app.config at call time so a test or
    restart can change it without this module caching a stale value.
    Called with `_LOCK` held (from `_do_commit_locked`)."""
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
