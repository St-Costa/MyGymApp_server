"""Core ingestion logic shared by all four record types (sessions,
readiness events, scale weigh-ins, raw ECG recordings): hash verification,
raw write, idempotency check, and the parse-then-upsert step.

Factored out of the route handlers so it has no dependency on FastAPI/HTTP —
this is what makes reparse.py able to reuse the parse-then-upsert half
without pulling in a web framework. The route handlers (app/routes.py) are
thin adapters that turn HTTP request parts into calls here.

Steps 3-7 (see docs/sync-ingestion/SPEC.md "Request handling") are identical
across sessions/readiness/scale modulo which table/parser/id-field is
involved — `ingest_record` below is that shared shape, parameterized rather
than copy-pasted three times. `ingest_ecg` deliberately does *not* go through
`ingest_record` — see its own docstring for why (compressed-vs-decompressed
hashing, gunzip step, binary format). Each concrete `ingest_*` function is a
thin, type-specific wrapper so callers (routes.py) still get a natural,
concretely-typed API per endpoint.
"""

import gzip
import hashlib
import logging
import os
import sqlite3
import traceback
import zlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from app.analysis import resting_hr_7day_average
from app.config import RAW_DIR
from app.ecg_analysis import analyze_ecg_recording
from app.parsing import (
    SessionParseError,
    parse_ecg_file,
    parse_readiness_file,
    parse_scale_weighin_file,
    parse_session_file,
    read_ecg_samples,
)
from app.storage import (
    get_stored_content_hash,
    record_parse_failure,
    upsert_ecg_analysis,
    upsert_parsed_ecg,
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


def resting_hr_for_hr_zones(
    conn: sqlite3.Connection, session_id: str, received_at: str
) -> float | None:
    """Looks up the anchor date for app.analysis.resting_hr_7day_average:
    the matching sessions.date if that session has already synced, falling
    back to received_at's own date otherwise (e.g. ECG arrives before its
    session — see ECG_SPEC.md; or a debug-* synthetic id, which never has a
    matching session at all — see ECG_SPEC.md point 5). Either anchor is a
    reasonable "around when this recording happened" date for a 7-day
    trailing average; there's no case where neither is available, since
    received_at is always set by the time this runs."""
    row = conn.execute("SELECT date FROM sessions WHERE id = ?", (session_id,)).fetchone()
    anchor_date = row[0] if row and row[0] else received_at[:10]
    return resting_hr_7day_average(conn, as_of_date=anchor_date)


def ingest_ecg(
    conn: sqlite3.Connection, envelope: Envelope, compressed_bytes: bytes
) -> IngestResult:
    """Steps 4-10 of POST /v1/ecg (docs/sync-ingestion/ECG_SPEC.md), plus
    running the docs/sync-ingestion/ECG_ANALYSIS_HANDOFF.md analysis (§1-§5)
    synchronously once the header parses — see the inner try/except below
    for why that's safe to do in-request.

    Deliberately its own function rather than a call into `ingest_record`:
    the hash there is computed over the *compressed* bytes (verified by the
    caller in app.routes, same as the other three — "hash what's on the
    wire"), but the raw write and parse step both need the *decompressed*
    bytes, and a bad/corrupt gunzip needs its own handling (still write to
    disk for forensics, log loudly, but never crash the request) that the
    generic helper has no reason to know about.
    """
    stored_hash = get_stored_content_hash(conn, envelope.record_id, table="ecg_recordings")

    if stored_hash is not None and stored_hash == envelope.content_hash:
        # Step 4: retried duplicate delivery — safe no-op.
        return IngestResult(record_id=envelope.record_id, status="duplicate")

    if stored_hash is not None:
        # Step 5: a resend for a known sessionId with a *different* hash is
        # unexpected (the phone captures ECG exactly once, never rewrites) —
        # log it distinctly from the duplicate case above, but still treat
        # as an overwrite rather than inventing a new rejection path.
        logger.warning(
            "unexpected ECG resend for session %s with a different contentHash "
            "(stored=%s, incoming=%s) — overwriting",
            envelope.record_id,
            stored_hash,
            envelope.content_hash,
        )

    received_at = datetime.now(UTC).isoformat()

    # Step 6: gunzip the verified-good compressed bytes. Shouldn't fail if the
    # hash check (step 3, in app.routes) passed — the hash covers exactly
    # what we're gunzipping here — but per the raw-first principle a corrupt
    # stream must still never fail the request. Fall back to storing the
    # compressed bytes as-is (still forensically useful) and record this the
    # same way as any other step 9 parse failure, rather than inventing a
    # separate error path.
    try:
        raw_bytes = gzip.decompress(compressed_bytes)
        target_path = envelope.rel_path
    except (gzip.BadGzipFile, zlib.error, EOFError):
        logger.exception(
            "gunzip failed for ECG session %s despite passing hash check "
            "(corrupted client-side gzip?) — storing compressed bytes as-is",
            envelope.record_id,
        )
        raw_bytes = compressed_bytes
        target_path = f"{envelope.rel_path}.gunzip-failed"

    # Step 8: write the bytes (decompressed in the normal case) — the raw
    # store stays directly usable without any reader needing to know it was
    # ever gzipped in transit.
    write_raw_file(raw_bytes, target_path)

    # Step 9, wrapped so a bad/unrecognized header (or the gunzip failure
    # above) can never fail the request — the raw file is already safely
    # stored, which is what matters.
    try:
        parsed = replace(parse_ecg_file(raw_bytes), id=envelope.record_id)
        upsert_parsed_ecg(
            conn,
            parsed,
            content_hash=envelope.content_hash,
            raw_path=target_path,
            received_at=received_at,
        )
    except SessionParseError:
        logger.exception("failed to parse ECG recording %s at %s", envelope.record_id, target_path)
        record_parse_failure(
            conn,
            table="ecg_recordings",
            record_type="ecg_recordings",
            record_id=envelope.record_id,
            content_hash=envelope.content_hash,
            raw_path=target_path,
            received_at=received_at,
            error=traceback.format_exc(),
        )
    else:
        # Run the ECG_ANALYSIS_HANDOFF.md analysis (§1-§5) right away, in the
        # same request — only reachable once the header parsed successfully
        # above, since analysis needs a valid sample_rate. Its own try/except:
        # a bug or edge case in the analysis math must never take down an
        # otherwise-successful upload — the header row (just written) already
        # makes this recording reparseable/re-analyzable later via
        # app.storage.get_unanalyzed_ecg_recordings regardless of what
        # happens here.
        try:
            samples = read_ecg_samples(raw_bytes)
            resting_hr = resting_hr_for_hr_zones(conn, envelope.record_id, received_at)
            result = analyze_ecg_recording(
                envelope.record_id,
                samples,
                parsed.sample_rate,
                file_size_bytes=len(raw_bytes),
                resting_hr=resting_hr,
            )
            upsert_ecg_analysis(conn, result, analyzed_at=received_at)
        except Exception:
            logger.exception(
                "ECG analysis failed for session %s at %s (recording is still stored; "
                "will remain in the analyzed_at IS NULL queue for retry)",
                envelope.record_id,
                target_path,
            )

    # Unlike sessions/readiness/scale, the spec calls for a plain "stored"
    # here even on the different-hash-overwrite path (point 4/step 5 in
    # ECG_SPEC.md) — no "updated" status for this endpoint's response contract.
    return IngestResult(record_id=envelope.record_id, status="stored")
