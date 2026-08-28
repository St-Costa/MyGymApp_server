"""HTTP route handlers.

Thin adapter layer: parses multipart parts into an Envelope, runs the steps
from docs/sync-ingestion/SPEC.md ("Request handling — do these steps in
order, exactly") in order, and delegates the actual work to app.ingestion.

Four record types (sessions, readiness events, scale weigh-ins, raw ECG
recordings — see docs/sync-ingestion/{SPEC,READINESS_SPEC,SCALE_SPEC,
ECG_SPEC}.md) share this exact request-handling shape, differing only in the
envelope's id field name, any extra required envelope fields, the ingest
function, and the response's id key. `_handle_ingest` factors that shape out
once; each route stays its own concrete, named endpoint (per the original
spec's "no speculative generalization" guidance) rather than a single
generic "record ingestion" route.
"""

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import asdict

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response

from app.analysis import (
    acwr_daily_trend,
    all_time_tonnage_pr,
    compute_acwr_trend,
    compute_cardio_trend_card,
    compute_rpe_acwr_trend,
    recent_heaviest_set_pr,
    resting_hr_weekly_trend,
    round_floats,
    rpe_acwr_daily_trend,
    scale_weekly_trend,
    session_load_trend,
    vo2max_4week_trend,
)
from app.config import DB_PATH
from app.git_backup import GitBackupError, commit_raw_store, format_commit_message
from app.ingestion import (
    Envelope,
    HashMismatchError,
    IngestResult,
    ingest_ecg,
    ingest_readiness,
    ingest_scale_weighin,
    ingest_session,
    verify_content_hash,
)
from app.repo import (
    RepoPathError,
    build_manifest,
    ingest_repo_delete,
    ingest_repo_upsert,
    read_live_file,
)
from app.security import require_valid_bearer_token
from app.storage import connect

logger = logging.getLogger("mygymapp.routes")

router = APIRouter()

# app.analysis.round_floats, aliased for brevity at the call sites below —
# every /v1/analysis/* handler runs its asdict()'d result through this so the
# JSON export is capped at 2 decimals (see the helper's docstring).
_round_floats = round_floats

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
    commit_count_key: str,
    extra_required_fields: tuple[str, ...] = (),
    include_steps: bool = False,
) -> JSONResponse:
    """Steps 1-7 minus auth (already enforced by the route's dependency).
    `id_field` is the envelope JSON key holding the record id
    (sessionId/eventId/weighInId/sessionId-for-ecg); `response_id_key` is
    what to call it in the JSON response, mirroring the field name the
    sender used. `extra_required_fields` covers envelope keys only some
    record types have (e.g. ECG's `encoding`, see ECG_SPEC.md)."""
    # Step 2: envelope validation.
    try:
        envelope_data = json.loads(envelope)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail={"error": f"malformed envelope JSON: {exc}"}
        ) from exc

    missing = [
        f
        for f in (id_field, *REQUIRED_ENVELOPE_FIELDS, *extra_required_fields)
        if not envelope_data.get(f)
    ]
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
        # SYNC.md "Daily step average": the phone always sends these two keys
        # (explicit JSON null when there's no data), but read with .get()
        # rather than indexing — a sender that omits them entirely should
        # degrade to "no data available", not 400, same leniency as the rest
        # of this envelope's optional fields.
        steps_avg_per_day=envelope_data.get("stepsAvgPerDay") if include_steps else None,
        steps_days_spanned=envelope_data.get("stepsDaysSpanned") if include_steps else None,
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

    # docs/backup-server-brief.md Task 1.2: one commit per successful request,
    # after the raw file(s) are durably on disk. A safe-no-op "duplicate"
    # wrote nothing, so `git add -A` stages nothing and commit_raw_store()
    # returns False without a commit — no need to special-case it here. A
    # genuine git failure (disk full mid-commit, corrupt index) is a 500:
    # the raw file is on disk but this request didn't cross the durability
    # boundary; the client retries and the next push's `git add -A` sweeps
    # in the uncommitted file.
    try:
        commit_raw_store(format_commit_message(**{commit_count_key: 1}))
    except GitBackupError as exc:
        logger.exception("git commit failed after ingesting %s", env.record_id)
        raise HTTPException(
            status_code=500, detail={"error": f"raw file stored but not committed: {exc}"}
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
        commit_count_key="sessions",
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
        commit_count_key="readiness",
        include_steps=True,
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
        commit_count_key="scale",
    )


@router.post("/v1/ecg", dependencies=[Depends(require_valid_bearer_token)])
async def post_ecg(
    envelope: str = Form(...),
    file: UploadFile = File(...),  # noqa: B008
) -> JSONResponse:
    """See docs/sync-ingestion/ECG_SPEC.md. Otherwise identical in shape to
    the other three endpoints — `_handle_ingest` verifies the hash over
    `file`'s raw (still gzip-compressed) bytes exactly as received, per
    ECG_SPEC.md point 2 ("hash what's on the wire", just compressed here) —
    the gunzip + magic validation happen one layer down in ingest_ecg, only
    after that hash check has already passed."""
    return await _handle_ingest(
        envelope=envelope,
        file=file,
        id_field="sessionId",
        response_id_key="sessionId",
        ingest_fn=ingest_ecg,
        commit_count_key="ecg",
        extra_required_fields=("encoding",),
    )


# ---------------------------------------------------------------------------
# Repo files: exercises/routines (docs/backup-server-brief.md Tasks 2 & 3).
#
# The fifth record type — bidirectional. POST /v1/repo upserts/deletes one
# exercises/*.md or routines/*.md file (soft-delete to deleted/, one git
# commit per successful push); GET /v1/manifest + GET /v1/file are the
# pull-only restore path the phone's "Ripristina dal server" flow uses.
# ---------------------------------------------------------------------------

_REQUIRED_REPO_ENVELOPE_FIELDS = ("relPath", "op", "contentHash")


@router.post("/v1/repo", dependencies=[Depends(require_valid_bearer_token)])
async def post_repo(
    envelope: str = Form(...),
    file: UploadFile | None = File(default=None),  # absent for op == "delete"  # noqa: B008
) -> JSONResponse:
    """docs/backup-server-brief.md §2. `op: "upsert"` needs a `file` part;
    `op: "delete"` doesn't. Same bearer auth, hash check, tolerant parse,
    and one-commit-per-request boundary as the other four endpoints."""
    try:
        envelope_data = json.loads(envelope)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail={"error": f"malformed envelope JSON: {exc}"}
        ) from exc

    missing = [f for f in _REQUIRED_REPO_ENVELOPE_FIELDS if not envelope_data.get(f)]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"error": f"envelope missing required field(s): {', '.join(missing)}"},
        )

    rel_path = envelope_data["relPath"]
    op = envelope_data["op"]
    content_hash = envelope_data["contentHash"]

    if op not in ("upsert", "delete"):
        raise HTTPException(
            status_code=422, detail={"error": f"unknown op: {op!r} (expected upsert|delete)"}
        )

    try:
        with connect(DB_PATH) as conn:
            if op == "upsert":
                if file is None:
                    raise HTTPException(
                        status_code=422,
                        detail={"error": "op 'upsert' requires a 'file' part"},
                    )
                raw_bytes = await file.read()
                try:
                    result = ingest_repo_upsert(
                        conn,
                        rel_path=rel_path,
                        content_hash=content_hash,
                        raw_bytes=raw_bytes,
                    )
                except RepoPathError as exc:
                    raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc
                except ValueError as exc:  # hash mismatch
                    raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc
            else:
                try:
                    result = ingest_repo_delete(conn, rel_path=rel_path)
                except RepoPathError as exc:
                    raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc
    except OSError as exc:
        logger.exception("failed to persist repo file %s", rel_path)
        raise HTTPException(
            status_code=500, detail={"error": f"failed to persist repo file: {exc}"}
        ) from exc

    # §2.2 step 5 / §2.3 step 4: a duplicate upsert or an already-absent
    # delete wrote nothing and must not commit.
    if result.status in ("duplicate", "already_absent"):
        return JSONResponse({"relPath": result.rel_path, "status": result.status})

    note = None
    if result.status == "deleted":
        note = f"delete {rel_path} → deleted/"
    try:
        commit_raw_store(format_commit_message(repo=1, note=note))
    except GitBackupError as exc:
        logger.exception("git commit failed after repo %s of %s", op, rel_path)
        raise HTTPException(
            status_code=500, detail={"error": f"repo file stored but not committed: {exc}"}
        ) from exc

    return JSONResponse({"relPath": result.rel_path, "status": result.status})


@router.get("/v1/manifest", dependencies=[Depends(require_valid_bearer_token)])
def get_manifest() -> dict:
    """§3.1: content hash of every live file under data/raw/ (everything
    except deleted/ and .git/), keyed by relPath."""
    return build_manifest()


@router.get("/v1/file", dependencies=[Depends(require_valid_bearer_token)])
def get_file(relPath: str = Query(...)) -> Response:  # noqa: N803 — matches the wire param name
    """§3.2: raw bytes of one live file, with an X-Content-SHA256 header.
    422 if relPath fails the safety check, 404 if it isn't a live file."""
    try:
        raw_bytes, sha = read_live_file(relPath)
    except RepoPathError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"error": f"not a live file: {relPath}"}
        ) from exc

    media_type = "text/markdown" if relPath.endswith(".md") else "application/octet-stream"
    return Response(
        content=raw_bytes,
        media_type=media_type,
        headers={"X-Content-SHA256": sha},
    )


# ---------------------------------------------------------------------------
# Analysis endpoints (docs/sync-ingestion/ANALYSIS_SPEC.md).
#
# Read-only: all query app.analysis functions against the existing SQLite
# layer, never touch the raw-first ingestion path above. `today` is an
# optional ISO date override (mainly for testing/backfilling a specific
# date rather than always "now") — omit it to analyze as of the current date.
# ---------------------------------------------------------------------------


@router.get("/v1/analysis/cardio-trend", dependencies=[Depends(require_valid_bearer_token)])
def get_cardio_trend(today: str | None = Query(default=None)) -> dict:
    """§11.1 + §11.2: 4-week cardio trend card with semaphores and
    self-diagnosis alert strings."""
    with connect(DB_PATH) as conn:
        card = compute_cardio_trend_card(conn, today=today)
    return _round_floats(asdict(card))


@router.get("/v1/analysis/acwr-trend", dependencies=[Depends(require_valid_bearer_token)])
def get_acwr_trend(today: str | None = Query(default=None)) -> dict:
    """§12: acute:chronic tonnage workload ratio, 7-day vs 28-day rolling mean."""
    with connect(DB_PATH) as conn:
        trend = compute_acwr_trend(conn, today=today)
    return _round_floats(asdict(trend))


@router.get("/v1/analysis/acwr-daily-trend", dependencies=[Depends(require_valid_bearer_token)])
def get_acwr_daily_trend(today: str | None = Query(default=None)) -> list[dict]:
    """§12 chart series: 12-week window, one ACWR point per day."""
    with connect(DB_PATH) as conn:
        points = acwr_daily_trend(conn, today=today)
    return _round_floats([asdict(p) for p in points])


@router.get("/v1/analysis/session-load-trend", dependencies=[Depends(require_valid_bearer_token)])
def get_session_load_trend(today: str | None = Query(default=None)) -> list[dict]:
    """§13: session-RPE / Foster session-load, 12-week window."""
    with connect(DB_PATH) as conn:
        points = session_load_trend(conn, today=today)
    return _round_floats([asdict(p) for p in points])


@router.get("/v1/analysis/rpe-acwr-trend", dependencies=[Depends(require_valid_bearer_token)])
def get_rpe_acwr_trend(today: str | None = Query(default=None)) -> dict:
    """§13.1: acute:chronic session-load ratio, the "internal load" ACWR."""
    with connect(DB_PATH) as conn:
        trend = compute_rpe_acwr_trend(conn, today=today)
    return _round_floats(asdict(trend))


@router.get("/v1/analysis/rpe-acwr-daily-trend", dependencies=[Depends(require_valid_bearer_token)])
def get_rpe_acwr_daily_trend(today: str | None = Query(default=None)) -> list[dict]:
    """§13.1 chart series: 12-week window, one RPE-ACWR point per day."""
    with connect(DB_PATH) as conn:
        points = rpe_acwr_daily_trend(conn, today=today)
    return _round_floats([asdict(p) for p in points])


@router.get("/v1/analysis/resting-hr-trend", dependencies=[Depends(require_valid_bearer_token)])
def get_resting_hr_trend(today: str | None = Query(default=None)) -> list[dict]:
    """§11.3: 8-week rolling window, weekly median resting HR."""
    with connect(DB_PATH) as conn:
        buckets = resting_hr_weekly_trend(conn, today=today)
    return _round_floats([asdict(b) for b in buckets])


@router.get("/v1/analysis/vo2max-trend", dependencies=[Depends(require_valid_bearer_token)])
def get_vo2max_trend(today: str | None = Query(default=None)) -> list[dict]:
    """§11.3: 6-month window, 4-week median buckets."""
    with connect(DB_PATH) as conn:
        buckets = vo2max_4week_trend(conn, today=today)
    return _round_floats([asdict(b) for b in buckets])


@router.get("/v1/analysis/scale-trend", dependencies=[Depends(require_valid_bearer_token)])
def get_scale_trend(today: str | None = Query(default=None)) -> dict[str, list[dict]]:
    """§11.3: 8-week rolling window, weekly medians for weight/BMI/body-fat/lean-mass."""
    with connect(DB_PATH) as conn:
        trends = scale_weekly_trend(conn, today=today)
    return _round_floats(
        {metric: [asdict(b) for b in buckets] for metric, buckets in trends.items()}
    )


@router.get(
    "/v1/analysis/exercises/{exercise_id}/pr", dependencies=[Depends(require_valid_bearer_token)]
)
def get_exercise_pr(
    exercise_id: str,
    is_daily: bool = Query(default=False),
    is_warmup: bool = Query(default=False),
) -> dict:
    """§1.5: the two distinct PR definitions, kept as separate fields rather
    than merged into one "PR" concept — see the spec for why."""
    with connect(DB_PATH) as conn:
        all_time = all_time_tonnage_pr(conn, exercise_id)
        recent = recent_heaviest_set_pr(conn, exercise_id, is_daily=is_daily, is_warmup=is_warmup)
    return _round_floats(
        {
            "exerciseId": exercise_id,
            "allTimeTonnagePr": asdict(all_time) if all_time else None,
            "recentHeaviestSetPr": asdict(recent) if recent else None,
        }
    )
