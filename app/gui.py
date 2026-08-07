"""Server-rendered dashboard: HTML views over app.analysis, no auth.

Deliberately not behind the bearer token used by the POST/GET /v1/* API —
this server is only ever reachable via Tailscale Serve on a private tailnet
(see docs/sync-ingestion/SPEC.md's non-goals), so there's no meaningful
attacker model to defend against here, and adding a browser-side token flow
would be complexity with no real security benefit for a single-user tool.

Templates call app.analysis functions directly against a live connection
(no internal HTTP round-trip to /v1/analysis/*) — this is a second, simpler
consumer of the same analysis layer, not a proxy in front of the API.
"""

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.analysis import (
    all_cardio_metrics_weekly_trend,
    all_time_tonnage_pr,
    compute_cardio_trend_card,
    exercise_history,
    latest_readiness,
    list_exercises,
    readiness_ln_rmssd_trend,
    recent_heaviest_set_pr,
    resting_hr_weekly_trend,
    scale_weekly_trend,
    vo2max_4week_trend,
)
from app.config import DB_PATH
from app.storage import connect

router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, today: str | None = Query(default=None)):
    with connect(DB_PATH) as conn:
        cardio = compute_cardio_trend_card(conn, today=today)
        cardio_series = all_cardio_metrics_weekly_trend(conn, today=today)
        resting_hr = resting_hr_weekly_trend(conn, today=today)
        vo2max = vo2max_4week_trend(conn, today=today)
        scale = scale_weekly_trend(conn, today=today)
        readiness = latest_readiness(conn)
        readiness_trend = readiness_ln_rmssd_trend(conn, today=today)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "cardio": cardio,
            # cardio_series feeds a |tojson in the template — dataclasses
            # aren't JSON-serializable by Jinja2's tojson filter, so convert
            # up front (WeeklyBucket -> plain dict) rather than in-template.
            "cardio_series": {k: [asdict(b) for b in v] for k, v in cardio_series.items()},
            "resting_hr_trend": resting_hr,
            "vo2max_trend": vo2max,
            "scale_trend": scale,
            "readiness": readiness,
            "readiness_trend": [asdict(p) for p in readiness_trend],
        },
    )


@router.get("/exercises", response_class=HTMLResponse)
def exercises_list(request: Request):
    with connect(DB_PATH) as conn:
        exercises = list_exercises(conn)

    return templates.TemplateResponse(request, "exercises.html", {"exercises": exercises})


@router.get("/exercises/{exercise_id}", response_class=HTMLResponse)
def exercise_detail(request: Request, exercise_id: str, today: str | None = Query(default=None)):
    with connect(DB_PATH) as conn:
        all_time = all_time_tonnage_pr(conn, exercise_id)
        recent = recent_heaviest_set_pr(conn, exercise_id, is_daily=False, is_warmup=False)
        recent_warmup = recent_heaviest_set_pr(conn, exercise_id, is_daily=False, is_warmup=True)
        recent_daily = recent_heaviest_set_pr(conn, exercise_id, is_daily=True, is_warmup=False)
        history = exercise_history(conn, exercise_id, today=today)
        # Best-effort display name: pull it from any set we can find.
        name_row = conn.execute(
            """SELECT exercise_name FROM exercise_sets
               WHERE exercise_id = ? AND exercise_name IS NOT NULL LIMIT 1""",
            (exercise_id,),
        ).fetchone()

    return templates.TemplateResponse(
        request,
        "exercise_detail.html",
        {
            "exercise_id": exercise_id,
            "exercise_name": name_row[0] if name_row else exercise_id,
            "all_time_pr": all_time,
            "recent_pr": recent,
            "recent_warmup_pr": recent_warmup,
            "recent_daily_pr": recent_daily,
            "history": [asdict(p) for p in history],
        },
    )
