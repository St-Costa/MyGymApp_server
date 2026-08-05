"""Core ingestion logic for one session: hash verification, raw write,
idempotency check, and the parse-then-upsert step.

Factored out of the route handler so it has no dependency on FastAPI/HTTP —
this is what makes reparse.py able to reuse the parse-then-upsert half
without pulling in a web framework. The route handler (app/routes.py) is a
thin adapter that turns HTTP request parts into calls here.
"""

import hashlib
import logging
import os
import sqlite3
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import RAW_DIR
from app.parsing import SessionParseError, parse_session_file
from app.storage import (
    get_stored_content_hash,
    record_unparsed_session,
    upsert_parsed_session,
)

logger = logging.getLogger("mygymapp.ingestion")


class HashMismatchError(ValueError):
    """The envelope's contentHash doesn't match the actual bytes received."""


@dataclass
class Envelope:
    session_id: str
    rel_path: str
    content_hash: str  # "sha256:<hex>", as received
    app_version: str
    client_sent_at: str


@dataclass
class IngestResult:
    session_id: str
    status: str  # "stored" | "updated" | "duplicate"


def compute_content_hash(raw_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


def verify_content_hash(raw_bytes: bytes, envelope: Envelope) -> None:
    """Step 3. Raises HashMismatchError without writing anything — this must
    run before any disk/DB write so a corrupted upload never gets persisted."""
    actual = compute_content_hash(raw_bytes)
    if actual != envelope.content_hash:
        raise HashMismatchError(
            f"contentHash mismatch: envelope said {envelope.content_hash}, computed {actual}"
        )


def write_raw_file(raw_bytes: bytes, rel_path: str) -> Path:
    """Step 5. Writes the raw bytes to RAW_DIR/<relPath>, creating parent dirs
    as needed, and fsyncs before returning so the write is durable before the
    caller proceeds to step 6 (parse). This is the durable source of truth —
    see docs/sync-ingestion/SPEC.md "raw-first, parse-second"."""
    target = RAW_DIR / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)

    # Write + explicit flush/fsync rather than a plain Path.write_bytes(), so
    # a crash immediately after this call can't leave a truncated file — the
    # raw store is the one thing this server is never allowed to lose.
    with open(target, "wb") as f:
        f.write(raw_bytes)
        f.flush()
        os.fsync(f.fileno())

    return target


def ingest_session(conn: sqlite3.Connection, envelope: Envelope, raw_bytes: bytes) -> IngestResult:
    """Steps 4-7 of POST /v1/sessions, after auth (step 1), envelope
    validation (step 2), and hash verification (step 3) have already passed.
    """
    stored_hash = get_stored_content_hash(conn, envelope.session_id)

    if stored_hash is not None and stored_hash == envelope.content_hash:
        # Step 4, case 2: retried duplicate delivery. Safe no-op — the sender
        # just didn't see the previous success response.
        return IngestResult(session_id=envelope.session_id, status="duplicate")

    is_update = stored_hash is not None  # case 3: existing session, new content

    # Step 5 — must succeed and be durable before we touch the database at all.
    write_raw_file(raw_bytes, envelope.rel_path)

    received_at = datetime.now(UTC).isoformat()

    # Step 6, wrapped so a parser bug/unexpected shape can never fail the
    # request — the raw file above is already safe, which is what matters.
    try:
        parsed = parse_session_file(raw_bytes)
        upsert_parsed_session(
            conn,
            parsed,
            content_hash=envelope.content_hash,
            raw_path=envelope.rel_path,
            received_at=received_at,
        )
    except SessionParseError:
        logger.exception("failed to parse session %s at %s", envelope.session_id, envelope.rel_path)
        record_unparsed_session(
            conn,
            session_id=envelope.session_id,
            content_hash=envelope.content_hash,
            raw_path=envelope.rel_path,
            received_at=received_at,
            error=traceback.format_exc(),
        )

    return IngestResult(session_id=envelope.session_id, status="updated" if is_update else "stored")
