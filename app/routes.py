"""HTTP route handlers.

Thin adapter layer: parses multipart parts into an Envelope, runs the steps
from docs/sync-ingestion/SPEC.md ("Request handling — do these steps in
order, exactly") in order, and delegates the actual work to app.ingestion.

Three record types (sessions, readiness events, scale weigh-ins — see
docs/sync-ingestion/{SPEC,READINESS_SPEC,SCALE_SPEC}.md) share this exact
request-handling shape, differing only in the envelope's id field name, the
ingest function, and the response's id key. `_handle_ingest` factors that
shape out once; each route stays its own concrete, named endpoint (per the
original spec's "no speculative generalization" guidance) rather than a
single generic "record ingestion" route.
"""

import json
import logging
import sqlite3
from collections.abc import Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.config import DB_PATH
from app.ingestion import (
    Envelope,
    HashMismatchError,
    IngestResult,
    ingest_readiness,
    ingest_scale_weighin,
    ingest_session,
    verify_content_hash,
)
from app.security import require_valid_bearer_token
from app.storage import connect

logger = logging.getLogger("mygymapp.routes")

router = APIRouter()

REQUIRED_ENVELOPE_FIELDS = ("relPath", "contentHash", "appVersion", "clientSentAt")


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


async def _handle_ingest(
    *,
    envelope: str,
    file: UploadFile,
    id_field: str,
    response_id_key: str,
    ingest_fn: Callable[[sqlite3.Connection, Envelope, bytes], IngestResult],
) -> JSONResponse:
    """Steps 1-7 minus auth (already enforced by the route's dependency).
    `id_field` is the envelope JSON key holding the record id
    (sessionId/eventId/weighInId); `response_id_key` is what to call it in
    the JSON response, mirroring the field name the sender used."""
    # Step 2: envelope validation.
    try:
        envelope_data = json.loads(envelope)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail={"error": f"malformed envelope JSON: {exc}"}
        ) from exc

    missing = [f for f in (id_field, *REQUIRED_ENVELOPE_FIELDS) if not envelope_data.get(f)]
    if missing:
        raise HTTPException(
            status_code=400,
            detail={"error": f"envelope missing required field(s): {', '.join(missing)}"},
        )
    # completedAt-style empty strings are fine elsewhere, but contentHash must
    # actually look like "sha256:<hex>" or step 3 can't do a meaningful compare.
    if not envelope_data["contentHash"].startswith("sha256:"):
        raise HTTPException(
            status_code=400, detail={"error": "contentHash must be prefixed with 'sha256:'"}
        )

    env = Envelope(
        record_id=envelope_data[id_field],
        rel_path=envelope_data["relPath"],
        content_hash=envelope_data["contentHash"],
        app_version=envelope_data["appVersion"],
        client_sent_at=envelope_data["clientSentAt"],
    )

    raw_bytes = await file.read()

    # Step 3: hash verification. Must happen before any write.
    try:
        verify_content_hash(raw_bytes, env)
    except HashMismatchError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc

    # Steps 4-7: idempotency check, raw write, parse-and-upsert, response.
    try:
        with connect(DB_PATH) as conn:
            result = ingest_fn(conn, env, raw_bytes)
    except OSError as exc:
        # Step 5 failure mode: disk full, permissions, etc. Must not proceed
        # to step 6 or record partial state — connect()'s rollback-on-error
        # already guarantees no DB write lands from this request.
        logger.exception("failed to write raw file for %s", env.record_id)
        raise HTTPException(
            status_code=500, detail={"error": f"failed to persist raw file: {exc}"}
        ) from exc

    return JSONResponse({response_id_key: result.record_id, "status": result.status})


@router.post("/v1/sessions", dependencies=[Depends(require_valid_bearer_token)])
async def post_session(
    envelope: str = Form(...),
    file: UploadFile = File(...),  # noqa: B008 — idiomatic FastAPI, not a mutable-default bug
) -> JSONResponse:
    return await _handle_ingest(
        envelope=envelope,
        file=file,
        id_field="sessionId",
        response_id_key="sessionId",
        ingest_fn=ingest_session,
    )


@router.post("/v1/readiness", dependencies=[Depends(require_valid_bearer_token)])
async def post_readiness(
    envelope: str = Form(...),
    file: UploadFile = File(...),  # noqa: B008
) -> JSONResponse:
    return await _handle_ingest(
        envelope=envelope,
        file=file,
        id_field="eventId",
        response_id_key="eventId",
        ingest_fn=ingest_readiness,
    )


@router.post("/v1/scale-weighins", dependencies=[Depends(require_valid_bearer_token)])
async def post_scale_weighin(
    envelope: str = Form(...),
    file: UploadFile = File(...),  # noqa: B008
) -> JSONResponse:
    return await _handle_ingest(
        envelope=envelope,
        file=file,
        id_field="weighInId",
        response_id_key="weighInId",
        ingest_fn=ingest_scale_weighin,
    )
