"""Background worker that runs ECG waveform analysis off the request path.

`POST /v1/ecg` used to run the full R-peak/HRV/DFA/SampEn analysis
synchronously inside the handler — seconds of CPU the phone waited for after
every workout. Now the handler only fsyncs the bytes, commits the header row,
enqueues `(session_id, raw_path)`, and returns `{"status": "stored"}`; this
drains that queue with the same analyze_one function analyze_ecg.py uses.

Failure domains stay split exactly as before: an analysis bug or edge case is
logged and the recording stays in the `analyzed_at IS NULL` queue for a later
`analyze_ecg.py --stale` retry — the upload itself already succeeded. The queue
is in-memory: a restart drops whatever hadn't been analyzed, and the same
`--stale`/`--all` script rebuilds it from the raw files.

The engine is resolved through app.ingestion (the live namespace), not
analyze_ecg's own binding — both are the same function object in production,
but tests simulate upload-time failures by patching app.ingestion, and the
worker must honor that patch so a simulated failure stays failed.
"""

import logging
import queue
import threading
import time

from analyze_ecg import analyze_one
from app.config import DB_PATH
from app.storage import connect

logger = logging.getLogger("mygymapp.ecg_worker")

# (session_id, raw_path) — raw_path is the relPath actually stored
# (decompressed normally, `.gunzip-failed` on a corrupt stream).
_queue: queue.Queue[tuple[str, str]] = queue.Queue()
_worker_thread: threading.Thread | None = None
_stop = threading.Event()


def enqueue_ecg_analysis(session_id: str, raw_path: str) -> None:
    """Called by the /v1/ecg handler after the header row is committed.
    Non-blocking."""
    _queue.put((session_id, raw_path))


# Put on the queue by stop_worker() to break _worker_loop out of its
# blocking get() immediately instead of waiting a poll interval.
_SENTINEL = ("", "")


def _worker_loop() -> None:
    import app.ingestion

    while not _stop.is_set():
        item = _queue.get()
        if item == _SENTINEL:
            _queue.task_done()
            break
        session_id, raw_path = item
        try:
            with connect(DB_PATH) as conn:
                analyze_one(
                    conn,
                    session_id,
                    raw_path,
                    analyze_fn=app.ingestion.analyze_ecg_recording,
                )
        except Exception:
            # Same contract as the old in-request try/except: never take down
            # the worker (or the upload, which already returned) — the raw
            # file and header row are safe, analyze_ecg.py retries later.
            logger.exception("ecg analysis worker failed on %s", session_id)
        finally:
            _queue.task_done()


def start_worker() -> None:
    """Start the background analysis worker. Idempotent. Called from the app
    lifespan (app/main.py)."""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _stop.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="ecg-analysis-worker", daemon=True)
    _worker_thread.start()
    logger.info("started ecg analysis worker")


def drain(*, timeout: float = 30.0) -> None:
    """Block until the queue is empty (used by the tests). Best-effort under
    `timeout` — the default covers the slowest fixture (a 90s recording's
    full neurokit pass takes seconds)."""
    # queue.join() has no timeout; poll unfinished_tasks instead.
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)


def stop_worker(*, timeout: float = 30.0) -> None:
    """Drain, then stop the worker thread. Called on app shutdown."""
    drain(timeout=timeout)
    _stop.set()
    _queue.put(_SENTINEL)  # break the blocking get() right away
    thread = _worker_thread
    if thread is not None:
        thread.join(timeout=timeout)
