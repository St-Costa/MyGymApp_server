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
    estimated_maintenance_calories,
    exercise_history,
    exercise_ids_trained_this_week,
    hr_zones_weekly_trend,
    list_exercises,
    muscle_group_weekly_trend,
    progressive_overload_verdict,
    readiness_daily_trend,
    recent_heaviest_set_pr,
    resting_hr_weekly_trend,
    rpe_acwr_daily_trend,
    scale_weekly_trend,
    session_calories_trend,
    session_load_trend,
    session_trimp_trend,
    steps_avg_per_day_weekly_trend,
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
        steps_avg_per_day_trend = steps_avg_per_day_weekly_trend(conn, today=today)
        readiness_trend = readiness_daily_trend(conn, today=today)
        acwr_trend = acwr_daily_trend(conn, today=today)
        session_load = session_load_trend(conn, today=today)
        rpe_acwr_trend = rpe_acwr_daily_trend(conn, today=today)
        session_calories = session_calories_trend(conn, today=today)
        session_trimp = session_trimp_trend(conn, today=today)
        maintenance = estimated_maintenance_calories(conn, today=today)

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
            "steps_avg_per_day_trend": [asdict(b) for b in steps_avg_per_day_trend],
            "readiness_trend": [asdict(p) for p in readiness_trend],
            "acwr_trend": [asdict(p) for p in acwr_trend],
            "session_load_trend": [asdict(p) for p in session_load],
            "rpe_acwr_trend": [asdict(p) for p in rpe_acwr_trend],
            "session_calories_trend": [asdict(p) for p in session_calories],
            "session_trimp_trend": [asdict(p) for p in session_trimp],
            "maintenance": asdict(maintenance) if maintenance else None,
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


# Display semaphore for muscle_group_weekly_trend's session_count — mirrors
# _frequency_semaphore in app.analysis exactly (0 -> RED, 1 -> YELLOW, 2+ ->
# GREEN); duplicated here as a tiny display-only lookup rather than
# importing the private function, since MuscleGroupWeek already carries
# frequency_semaphore precomputed — this only exists because the dashboard
# card needs the same color as a dict key, not a dataclass attribute lookup.
def _frequency_dot(session_count: int) -> str:
    if session_count <= 0:
        return "RED"
    if session_count == 1:
        return "YELLOW"
    return "GREEN"


# Dot color per OverloadMetricVerdict.verdict value, for the
# progressive-overload table cells — per user decision: growing green,
# stalled gray, fast RED (same severity as dropping — an implausible jump
# is flagged as hard a problem as losing strength, not a lesser caution),
# no dot at all when there's nothing to show (INSUFFICIENT_DATA, handled
# separately in _overload_cell).
_OVERLOAD_VERDICT_COLOR = {
    "UP_HEALTHY": "GREEN",
    "UP_TOO_FAST": "RED",
    "STALLED": "GRAY",
    "DOWN": "RED",
}


def _overload_cell(v) -> dict | None:
    """Formats one OverloadMetricVerdict for template display: dot color and
    a whole-percent rate (no decimals), or None when there's nothing to show
    (template renders "(Not enough data)" with no dot in that case)."""
    if v.verdict == "INSUFFICIENT_DATA" or v.percent_per_week is None:
        return None
    return {
        "color": _OVERLOAD_VERDICT_COLOR[v.verdict],
        "percent": f"{v.percent_per_week:+.0f}%",
    }


@router.get("/hypertrophy", response_class=HTMLResponse)
def hypertrophy(request: Request, today: str | None = Query(default=None)):
    """Current-week muscle-group volume/frequency (Schoenfeld-et-al.-derived
    semaphores) and progressive-overload verdicts for exercises trained this
    week — built on top of exercise_sets.bodypart. See app.analysis's module
    comments above muscle_group_weekly_trend and progressive_overload_verdict
    for the full reasoning/thresholds. "This week" is the calendar Monday..
    Sunday containing `today`, not a rolling 7-day window — same convention
    as exercise_ids_trained_this_week/muscle_group_weekly_trend's own
    Monday-keyed buckets."""
    with connect(DB_PATH) as conn:
        muscle_trend = muscle_group_weekly_trend(conn, today=today)
        trained_this_week = exercise_ids_trained_this_week(conn, today=today)
        exercises = list_exercises(conn)
        exercises_this_week = [e for e in exercises if e.exercise_id in trained_this_week]
        overload = [
            progressive_overload_verdict(conn, e.exercise_id, today=today)
            for e in exercises_this_week
        ]

    # Only the most recent week per bodypart, and only bodyparts actually
    # trained this week (set_count > 0) — per user decision, an empty card
    # for a muscle group not touched this week adds noise, not signal; the
    # historical trend for it is still available via muscle_trend if ever
    # needed, just not surfaced as a "this week" card.
    current_week = [
        {
            "bodypart": bodypart,
            "set_count": weeks[-1].set_count,
            "volume_color": weeks[-1].volume_semaphore,
            "session_count": weeks[-1].session_count,
            "frequency_color": _frequency_dot(weeks[-1].session_count),
        }
        for bodypart, weeks in sorted(muscle_trend.items())
        if weeks and weeks[-1].set_count > 0
    ]

    exercise_names = {e.exercise_id: e.exercise_name or e.exercise_id for e in exercises}
    overload_rows = [
        {
            "exercise_id": v.exercise_id,
            "exercise_name": exercise_names.get(v.exercise_id, v.exercise_id),
            "e1rm": _overload_cell(v.e1rm),
            "tonnage": _overload_cell(v.tonnage),
            "_sort_key": v.e1rm.percent_per_week or v.tonnage.percent_per_week or 0,
        }
        for v in overload
    ]
    # Biggest movers (by e1RM rate, falling back to tonnage) surface first —
    # a single sort key since the table shows one row per exercise, not one
    # per metric; e1RM and tonnage verdicts can still disagree within a row.
    overload_rows.sort(key=lambda r: r["_sort_key"], reverse=True)

    return templates.TemplateResponse(
        request,
        "hypertrophy.html",
        {
            "current_week": current_week,
            "overload_rows": overload_rows,
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
