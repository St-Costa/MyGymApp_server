"""HTTP route handlers.

Thin adapter layer: parses multipart parts into an Envelope, runs the steps
from docs/sync-ingestion/SPEC.md ("Request handling — do these steps in
order, exactly") in order, and delegates the actual work to app.ingestion.
Kept as one concrete handler per the spec's "no speculative generalization"
guidance — this is not a generic record-ingestion framework.
"""

import json
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.config import DB_PATH
from app.ingestion import Envelope, HashMismatchError, ingest_session, verify_content_hash
from app.security import require_valid_bearer_token
from app.storage import connect

logger = logging.getLogger("mygymapp.routes")

router = APIRouter()

REQUIRED_ENVELOPE_FIELDS = (
    "sessionId",
    "relPath",
    "contentHash",
    "appVersion",
    "clientSentAt",
)


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/v1/sessions", dependencies=[Depends(require_valid_bearer_token)])
async def post_session(
    envelope: str = Form(...),
    file: UploadFile = File(...),  # noqa: B008 — idiomatic FastAPI, not a mutable-default bug
) -> JSONResponse:
    # Step 2: envelope validation.
    try:
        envelope_data = json.loads(envelope)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail={"error": f"malformed envelope JSON: {exc}"}
        ) from exc

    missing = [f for f in REQUIRED_ENVELOPE_FIELDS if not envelope_data.get(f)]
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
        session_id=envelope_data["sessionId"],
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
            result = ingest_session(conn, env, raw_bytes)
    except OSError as exc:
        # Step 5 failure mode: disk full, permissions, etc. Must not proceed
        # to step 6 or record partial state — connect()'s rollback-on-error
        # already guarantees no DB write lands from this request.
        logger.exception("failed to write raw file for session %s", env.session_id)
        raise HTTPException(
            status_code=500, detail={"error": f"failed to persist raw file: {exc}"}
        ) from exc

    return JSONResponse({"sessionId": result.session_id, "status": result.status})
