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
from datetime import date as date_cls
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.analysis import (
    acwr_daily_trend,
    all_cardio_metrics_weekly_trend,
    all_time_tonnage_pr,
    compute_cardio_trend_card,
    ecg_advanced_metrics_weekly_trend,
    exercise_history,
    hr_zones_weekly_trend,
    list_exercises,
    readiness_daily_trend,
    recent_heaviest_set_pr,
    resting_hr_weekly_trend,
    rpe_acwr_daily_trend,
    scale_weekly_trend,
    session_calories_trend,
    session_load_trend,
    session_trimp_trend,
    vo2max_4week_trend,
)
from app.config import DB_PATH
from app.storage import connect
from app.weekly_report import build_weekly_report

router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, today: str | None = Query(default=None)):
    with connect(DB_PATH) as conn:
        cardio = compute_cardio_trend_card(conn, today=today)
        cardio_series = all_cardio_metrics_weekly_trend(conn, today=today)
        ecg_advanced_series = ecg_advanced_metrics_weekly_trend(conn, today=today)
        hr_zones_series = hr_zones_weekly_trend(conn, today=today)
        resting_hr = resting_hr_weekly_trend(conn, today=today)
        vo2max = vo2max_4week_trend(conn, today=today)
        scale = scale_weekly_trend(conn, today=today)
        readiness_trend = readiness_daily_trend(conn, today=today)
        acwr_trend = acwr_daily_trend(conn, today=today)
        session_load = session_load_trend(conn, today=today)
        rpe_acwr_trend = rpe_acwr_daily_trend(conn, today=today)
        session_calories = session_calories_trend(conn, today=today)
        session_trimp = session_trimp_trend(conn, today=today)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "cardio": cardio,
            # cardio_series feeds a |tojson in the template — dataclasses
            # aren't JSON-serializable by Jinja2's tojson filter, so convert
            # up front (WeeklyBucket -> plain dict) rather than in-template.
            "cardio_series": {k: [asdict(b) for b in v] for k, v in cardio_series.items()},
            "ecg_advanced_series": {
                k: [asdict(b) for b in v] for k, v in ecg_advanced_series.items()
            },
            "hr_zones_series": {k: [asdict(b) for b in v] for k, v in hr_zones_series.items()},
            "resting_hr_trend": resting_hr,
            "vo2max_trend": vo2max,
            "scale_trend": scale,
            "readiness_trend": [asdict(p) for p in readiness_trend],
            "acwr_trend": [asdict(p) for p in acwr_trend],
            "session_load_trend": [asdict(p) for p in session_load],
            "rpe_acwr_trend": [asdict(p) for p in rpe_acwr_trend],
            "session_calories_trend": [asdict(p) for p in session_calories],
            "session_trimp_trend": [asdict(p) for p in session_trimp],
        },
    )


@router.get("/weekly-report", response_class=HTMLResponse)
def weekly_report_preview(request: Request, week_end: str | None = Query(default=None)):
    """Manual preview of the deterministic weekly report content
    (app.weekly_report) — lets the user eyeball what a given week's ntfy
    notification would say before any send/schedule infrastructure exists.
    `week_end` (ISO date, the Sunday of the reported week) is optional and
    defaults to the most recently completed week, same as build_weekly_report."""
    parsed_week_end = date_cls.fromisoformat(week_end) if week_end else None
    with connect(DB_PATH) as conn:
        report = build_weekly_report(conn, week_end=parsed_week_end)

    return templates.TemplateResponse(request, "weekly_report.html", {"report": report})


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
