"""Core ingestion logic shared by all three record types (sessions,
readiness events, scale weigh-ins): hash verification, raw write,
idempotency check, and the parse-then-upsert step.

Factored out of the route handlers so it has no dependency on FastAPI/HTTP —
this is what makes reparse.py able to reuse the parse-then-upsert half
without pulling in a web framework. The route handlers (app/routes.py) are
thin adapters that turn HTTP request parts into calls here.

Steps 3-7 (see docs/sync-ingestion/SPEC.md "Request handling") are identical
across all three record types modulo which table/parser/id-field is
involved — `ingest_record` below is that shared shape, parameterized rather
than copy-pasted three times. Each concrete `ingest_*` function is a thin,
type-specific wrapper so callers (routes.py) still get a natural,
concretely-typed API per endpoint.
"""

import hashlib
import logging
import os
import sqlite3
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import RAW_DIR
from app.parsing import (
    SessionParseError,
    parse_readiness_file,
    parse_scale_weighin_file,
    parse_session_file,
)
from app.storage import (
    get_stored_content_hash,
    record_parse_failure,
    upsert_parsed_readiness,
    upsert_parsed_scale_weighin,
    upsert_parsed_session,
)

logger = logging.getLogger("mygymapp.ingestion")


class HashMismatchError(ValueError):
    """The envelope's contentHash doesn't match the actual bytes received."""


@dataclass
class Envelope:
    record_id: str
    rel_path: str
    content_hash: str  # "sha256:<hex>", as received
    app_version: str
    client_sent_at: str


@dataclass
class IngestResult:
    record_id: str
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


def ingest_record[T](
    conn: sqlite3.Connection,
    envelope: Envelope,
    raw_bytes: bytes,
    *,
    table: str,
    record_type: str,
    parse_fn: Callable[[bytes], T],
    upsert_fn: Callable[..., None],
) -> IngestResult:
    """Steps 4-7, generalized over which record type is being ingested.

    `table`/`record_type` name where idempotency/failure-tracking rows live
    (see app.storage._RECORD_TABLES and the parse_failures schema note for
    why both a table name and a separate type tag are needed — ids are only
    unique within their own record type, not globally).
    """
    stored_hash = get_stored_content_hash(conn, envelope.record_id, table=table)

    if stored_hash is not None and stored_hash == envelope.content_hash:
        # Step 4, case 2: retried duplicate delivery. Safe no-op — the sender
        # just didn't see the previous success response.
        return IngestResult(record_id=envelope.record_id, status="duplicate")

    is_update = stored_hash is not None  # case 3: existing record, new content

    # Step 5 — must succeed and be durable before we touch the database at all.
    write_raw_file(raw_bytes, envelope.rel_path)

    received_at = datetime.now(UTC).isoformat()

    # Step 6, wrapped so a parser bug/unexpected shape can never fail the
    # request — the raw file above is already safe, which is what matters.
    try:
        parsed = parse_fn(raw_bytes)
        upsert_fn(
            conn,
            parsed,
            content_hash=envelope.content_hash,
            raw_path=envelope.rel_path,
            received_at=received_at,
        )
    except SessionParseError:
        logger.exception(
            "failed to parse %s %s at %s", record_type, envelope.record_id, envelope.rel_path
        )
        record_parse_failure(
            conn,
            table=table,
            record_type=record_type,
            record_id=envelope.record_id,
            content_hash=envelope.content_hash,
            raw_path=envelope.rel_path,
            received_at=received_at,
            error=traceback.format_exc(),
        )

    return IngestResult(record_id=envelope.record_id, status="updated" if is_update else "stored")


def ingest_session(conn: sqlite3.Connection, envelope: Envelope, raw_bytes: bytes) -> IngestResult:
    """Steps 4-7 of POST /v1/sessions."""
    return ingest_record(
        conn,
        envelope,
        raw_bytes,
        table="sessions",
        record_type="sessions",
        parse_fn=parse_session_file,
        upsert_fn=upsert_parsed_session,
    )


def ingest_readiness(
    conn: sqlite3.Connection, envelope: Envelope, raw_bytes: bytes
) -> IngestResult:
    """Steps 4-7 of POST /v1/readiness (docs/sync-ingestion/READINESS_SPEC.md)."""
    return ingest_record(
        conn,
        envelope,
        raw_bytes,
        table="readiness_events",
        record_type="readiness_events",
        parse_fn=parse_readiness_file,
        upsert_fn=upsert_parsed_readiness,
    )


def ingest_scale_weighin(
    conn: sqlite3.Connection, envelope: Envelope, raw_bytes: bytes
) -> IngestResult:
    """Steps 4-7 of POST /v1/scale-weighins (docs/sync-ingestion/SCALE_SPEC.md)."""
    return ingest_record(
        conn,
        envelope,
        raw_bytes,
        table="scale_weighins",
        record_type="scale_weighins",
        parse_fn=parse_scale_weighin_file,
        upsert_fn=upsert_parsed_scale_weighin,
    )
