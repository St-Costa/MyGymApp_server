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
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import asdict

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
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
from app.git_backup import request_commit
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
    BULK_MAX_FILES,
    BULK_MAX_TOTAL_BYTES,
    RepoPathError,
    build_manifest,
    build_tarball,
    ingest_repo_bulk,
    ingest_repo_delete,
    ingest_repo_upsert,
    read_live_file,
    read_live_files,
)
from app.repo_worker import enqueue_parse
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
    # No auth (see docs/sync-ingestion/SPEC.md) — backs the app's
    # "Verifica connessione" button, which checks for {"ok": true}.
    return {"ok": True}


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
    include_sleep_quality: bool = False,
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

    # READINESS_SPEC.md: sleepQuality is an explicit envelope key — an int
    # 1..5 (rated) or literal null (non valutato). Unlike the steps fields,
    # a present-but-malformed value is a hard 400 (same rule the spec calls
    # out): coercing a bad rating to 0 or dropping it silently would corrupt
    # a subjective scale where every value carries meaning. A missing key is
    # still tolerated as "non valutato", matching the rest of the envelope.
    sleep_quality = None
    if include_sleep_quality and "sleepQuality" in envelope_data:
        sleep_quality = envelope_data["sleepQuality"]
        if sleep_quality is not None and (
            isinstance(sleep_quality, bool)
            or not isinstance(sleep_quality, int)
            or not 1 <= sleep_quality <= 5
        ):
            raise HTTPException(
                status_code=400,
                detail={"error": "sleepQuality must be null or an integer in 1..5"},
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
        sleep_quality=sleep_quality,
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

    # docs/backup/OPTIMIZATION.md point 1: the raw file is fsynced by now
    # (that's what "stored" means to the client, and always has been — the
    # 2xx never waited on the git commit conceptually). Hand the burst to
    # the debounced committer instead of forking `git` here; it folds this
    # write into one commit for the whole burst after ~3s of write silence.
    # A "duplicate" wrote nothing, so there's nothing to commit.
    if result.status != "duplicate":
        request_commit(**{commit_count_key: 1})

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
        include_sleep_quality=True,
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
    `op: "delete"` doesn't. Same bearer auth and hash check as the other
    four endpoints. The YAML parse into the SQL view runs off the request
    path (app.repo_worker), and the git commit is debounced
    (app.git_backup) — see docs/backup/OPTIMIZATION.md points 1 & 6. A 2xx
    means the raw bytes are fsynced + hash-verified, nothing more."""
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
        if op == "upsert":
            if file is None:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "op 'upsert' requires a 'file' part"},
                )
            raw_bytes = await file.read()
            try:
                result = ingest_repo_upsert(
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
                result = ingest_repo_delete(rel_path=rel_path)
            except RepoPathError as exc:
                raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc
    except OSError as exc:
        logger.exception("failed to persist repo file %s", rel_path)
        raise HTTPException(
            status_code=500, detail={"error": f"failed to persist repo file: {exc}"}
        ) from exc

    # §2.2 step 5 / §2.3 step 4: a duplicate upsert or an already-absent
    # delete wrote nothing — no commit, no parse.
    if result.status in ("duplicate", "already_absent"):
        return JSONResponse({"relPath": result.rel_path, "status": result.status})

    # Enqueue the SQL-view parse (off the request path) and hand the write
    # to the debounced committer.
    enqueue_parse(rel_path, content_hash, op)
    note = f"delete {rel_path} → deleted/" if result.status == "deleted" else None
    request_commit(repo=1, note=note)

    return JSONResponse({"relPath": result.rel_path, "status": result.status})


@router.post("/v1/repo/bulk", dependencies=[Depends(require_valid_bearer_token)])
async def post_repo_bulk(request: Request) -> JSONResponse:
    """docs/backup/OPTIMIZATION.md point 2: N repo upserts/deletes in one
    request, one debounced commit.

    multipart/form-data:
      - part `envelope` (application/json): a JSON array
        `[{"relPath": ..., "op": "upsert"|"delete", "contentHash": ...}, ...]`
      - parts `file_0`, `file_1`, … (any content type): raw bytes for the
        entry at that index, present iff that entry's op is "upsert".

    Response `200`:
      {"results": [{"relPath": ..., "status": "stored"|"duplicate"|
                    "deleted"|"already_absent"|"error", "error": ...?}, ...]}
    in the same order as `envelope`. A per-entry hash mismatch / bad path is
    that entry's `"error"`, not a request failure. `413` if the batch
    exceeds BULK_MAX_FILES files or BULK_MAX_TOTAL_BYTES total."""
    form = await request.form()
    envelope_raw = form.get("envelope")
    if not isinstance(envelope_raw, str):
        raise HTTPException(status_code=422, detail={"error": "missing 'envelope' JSON array part"})
    try:
        entries = json.loads(envelope_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail={"error": f"malformed envelope JSON: {exc}"}
        ) from exc
    if not isinstance(entries, list):
        raise HTTPException(status_code=422, detail={"error": "'envelope' must be a JSON array"})
    if len(entries) > BULK_MAX_FILES:
        raise HTTPException(
            status_code=413,
            detail={"error": f"too many entries: {len(entries)} > {BULK_MAX_FILES}"},
        )

    files_by_index: dict[int, bytes] = {}
    total = 0
    for i in range(len(entries)):
        part = form.get(f"file_{i}")
        if part is None or isinstance(part, str):
            continue
        data = await part.read()
        total += len(data)
        if total > BULK_MAX_TOTAL_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"error": f"batch exceeds {BULK_MAX_TOTAL_BYTES} bytes"},
            )
        files_by_index[i] = data

    results, touched, notes = ingest_repo_bulk(entries, files_by_index)

    if touched:
        note = "; ".join(notes) if notes else None
        request_commit(repo=touched, note=note)

    return JSONResponse(
        {
            "results": [
                {"relPath": r.rel_path, "status": r.status}
                | ({"error": r.error} if r.error else {})
                for r in results
            ]
        }
    )


@router.get("/v1/manifest", dependencies=[Depends(require_valid_bearer_token)])
def get_manifest() -> dict:
    """§3.1: content hash of every live file under data/raw/ (everything
    except deleted/ and .git/), keyed by relPath."""
    return build_manifest()


@router.post("/v1/repo/files", dependencies=[Depends(require_valid_bearer_token)])
async def post_repo_files(request: Request) -> Response:
    """docs/backup/OPTIMIZATION.md point 3: fetch many live files in one
    round-trip, for the "Verifica backup" read-back and a targeted restore.

    Request JSON: `{"relPaths": ["exercises/…", "readiness/…", …]}` (any
    live subtree, not just repo files — same as `GET /v1/file`).

    Response: `multipart/mixed`, one part per requested relPath in order.
    Each part carries `Content-Disposition: attachment; name="<relPath>"`,
    `X-Content-SHA256: sha256:…` (present only for files that exist), and
    `X-Status: present|absent`. `413` if `relPaths` exceeds BULK_MAX_FILES."""
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail={"error": f"malformed JSON body: {exc}"}
        ) from exc
    rel_paths = body.get("relPaths") if isinstance(body, dict) else None
    if not isinstance(rel_paths, list):
        raise HTTPException(status_code=422, detail={"error": 'body must be {"relPaths": [...]}'})
    if len(rel_paths) > BULK_MAX_FILES:
        raise HTTPException(
            status_code=413,
            detail={"error": f"too many relPaths: {len(rel_paths)} > {BULK_MAX_FILES}"},
        )

    fetched = read_live_files([str(p) for p in rel_paths])

    boundary = "repofiles-" + secrets.token_hex(16)
    chunks: list[bytes] = []
    for rel_path, raw_bytes, sha in fetched:
        headers = [
            f'Content-Disposition: attachment; name="{rel_path}"',
        ]
        if raw_bytes is None:
            headers.append("X-Status: absent")
            body_bytes = b""
        else:
            headers.append("X-Status: present")
            headers.append(f"X-Content-SHA256: {sha}")
            ctype = "text/markdown" if rel_path.endswith(".md") else "application/octet-stream"
            headers.append(f"Content-Type: {ctype}")
            body_bytes = raw_bytes
        chunks.append(
            f"--{boundary}\r\n".encode()
            + ("\r\n".join(headers) + "\r\n\r\n").encode()
            + body_bytes
            + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode())

    return Response(
        content=b"".join(chunks),
        media_type=f'multipart/mixed; boundary="{boundary}"',
    )


@router.get("/v1/repo/tarball", dependencies=[Depends(require_valid_bearer_token)])
def get_repo_tarball(since: str | None = Query(default=None)) -> Response:
    """docs/backup/OPTIMIZATION.md point 3: a gzip-compressed tar of every
    live file, for a from-scratch restore in one transfer. The
    `X-Manifest-SHA256` response header is the current manifest hash; pass
    it back as `?since=<hash>` next time and the tar is empty when nothing
    changed. Pull-only."""
    tar_gz, manifest_hash = build_tarball(since_manifest_hash=since)
    return Response(
        content=tar_gz,
        media_type="application/gzip",
        headers={
            "X-Manifest-SHA256": manifest_hash,
            "Content-Disposition": 'attachment; filename="repo-restore.tar.gz"',
        },
    )


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
