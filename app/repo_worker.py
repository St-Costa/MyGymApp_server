"""Background worker that parses synced repo files into the exercises/
routines SQL view, off the request path.

docs/backup/OPTIMIZATION.md point 6: `POST /v1/repo` used to do the YAML
parse + SQLite upsert synchronously inside the handler. The raw write and
the parse are separate failure domains (the raw file is the source of
truth; the SQL view is a re-derivable index — see docs/sync-ingestion/
SPEC.md), so the handler now just fsyncs the bytes, enqueues the relPath,
and returns. This drains that queue.

The queue is in-memory: a clean restart loses whatever hadn't been parsed
yet, but `reparse.py --type exercises|routines --stale` (or `--all`)
rebuilds the whole view from the raw files on disk, so nothing is actually
lost — the same recovery story as every other re-derivable table here.
"""

import logging
import queue
import threading
import time
from datetime import UTC, datetime

from app.config import DB_PATH
from app.repo import parse_repo_file_into_view
from app.storage import connect

logger = logging.getLogger("mygymapp.repo_worker")

# (rel_path, content_hash, op) — op is "upsert" or "delete".
_queue: queue.Queue[tuple[str, str, str]] = queue.Queue()
_worker_thread: threading.Thread | None = None
_stop = threading.Event()


def enqueue_parse(rel_path: str, content_hash: str, op: str) -> None:
    """Called by the /v1/repo handler after the raw file is durably
    written. Non-blocking. `op` is "upsert" (parse + upsert the row) or
    "delete" (mark the row deleted_at)."""
    _queue.put((rel_path, content_hash, op))


# Put on the queue by stop_worker() to break _worker_loop out of its
# blocking get() immediately instead of waiting a poll interval.
_SENTINEL = ("", "", "")


def _worker_loop() -> None:
    while not _stop.is_set():
        item = _queue.get()
        if item == _SENTINEL:
            _queue.task_done()
            break
        rel_path, content_hash, op = item
        try:
            with connect(DB_PATH) as conn:
                parse_repo_file_into_view(
                    conn,
                    rel_path=rel_path,
                    content_hash=content_hash,
                    op=op,
                    received_at=datetime.now(UTC).isoformat(),
                )
        except Exception:
            # A parser bug or unexpected shape must never take the worker
            # down — the raw file is safe, reparse.py can retry later.
            logger.exception("repo parse worker failed on %s (%s)", rel_path, op)
        finally:
            _queue.task_done()


def start_worker() -> None:
    """Start the background parse worker. Idempotent. Called from the app
    lifespan (app/main.py)."""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _stop.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="repo-parse-worker", daemon=True)
    _worker_thread.start()
    logger.info("started repo parse worker")


def drain(*, timeout: float = 10.0) -> None:
    """Block until the queue is empty (used by the tests and by shutdown so
    a clean restart doesn't drop pending parses). Best-effort under
    `timeout`."""
    # queue.join() has no timeout; poll unfinished_tasks instead.
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)


def stop_worker(*, timeout: float = 10.0) -> None:
    """Drain, then stop the worker thread. Called on app shutdown."""
    drain(timeout=timeout)
    _stop.set()
    _queue.put(_SENTINEL)  # break the blocking get() right away
    thread = _worker_thread
    if thread is not None:
        thread.join(timeout=timeout)
