"""FastAPI app entrypoint.

Binds to 127.0.0.1 only when run via `uvicorn app.main:app` for local dev
(the Makefile `dev` target does this) — see docs/sync-ingestion/SPEC.md:
this service must never be reachable other than via Tailscale Serve
reverse-proxying to localhost. The Docker image instead binds 0.0.0.0
inside the container, since the host-side port publish
(`-p 127.0.0.1:PORT:PORT`) is what enforces the same guarantee across the
container boundary — see Dockerfile.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import LOGS_DIR
from app.routes import router
from app.storage import init_db

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


app = FastAPI(title="MyGymApp Sync Server", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(HTTPException)
async def flatten_error_body(_request: Request, exc: HTTPException) -> JSONResponse:
    """The spec asks for error bodies shaped `{"error": "..."}` at the top
    level; FastAPI's default wraps HTTPException.detail as `{"detail": ...}`.
    Routes raise HTTPException(detail={"error": "..."}) and this unwraps it,
    so route code stays idiomatic FastAPI while the wire format matches spec."""
    body = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
    return JSONResponse(status_code=exc.status_code, content=body)
