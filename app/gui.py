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

from app import security
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
    latest_bodyweight_load,
    list_exercises,
    muscle_group_weekly_trend,
    progressive_overload_verdict,
    readiness_current_week,
    readiness_daily_trend,
    recent_prs_for_exercise,
    resting_hr_weekly_trend,
    round_floats,
    rpe_acwr_daily_trend,
    scale_weekly_trend,
    session_calories_trend,
    session_load_trend,
    session_trimp_trend,
    steps_avg_per_day_weekly_trend,
    stretch_weekly_trend,
    training_monotony_weekly_trend,
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
        ecg_advanced_series = ecg_advanced_metrics_weekly_trend(conn, today=today)
        hr_zones_series = hr_zones_weekly_trend(conn, today=today)
        resting_hr = resting_hr_weekly_trend(conn, today=today)
        vo2max = vo2max_4week_trend(conn, today=today)
        scale = scale_weekly_trend(conn, today=today)
        steps_avg_per_day_trend = steps_avg_per_day_weekly_trend(conn, today=today)
        readiness_trend = readiness_daily_trend(conn, today=today)
        readiness_week = readiness_current_week(conn, today=today)
        acwr_trend = acwr_daily_trend(conn, today=today)
        session_load = session_load_trend(conn, today=today)
        rpe_acwr_trend = rpe_acwr_daily_trend(conn, today=today)
        session_calories = session_calories_trend(conn, today=today)
        session_trimp = session_trimp_trend(conn, today=today)
        monotony_trend = training_monotony_weekly_trend(conn, today=today)
        maintenance = estimated_maintenance_calories(conn, today=today)
        exercise_analysis = _exercise_analysis_context(conn, today)

    # Every numeric value below is display-only and feeds either an inline
    # {{ }} or a |tojson chart series — round to 2 decimals at this single
    # boundary so the rendered page never shows a long floating-point tail
    # (see app.analysis.round_floats).
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        round_floats(
            {
                "cardio": asdict(cardio),
                # cardio_series feeds a |tojson in the template — dataclasses
                # aren't JSON-serializable by Jinja2's tojson filter, so convert
                # up front (WeeklyBucket -> plain dict) rather than in-template.
                "cardio_series": {k: [asdict(b) for b in v] for k, v in cardio_series.items()},
                "ecg_advanced_series": {
                    k: [asdict(b) for b in v] for k, v in ecg_advanced_series.items()
                },
                "hr_zones_series": {k: [asdict(b) for b in v] for k, v in hr_zones_series.items()},
                "resting_hr_trend": [asdict(b) for b in resting_hr],
                "vo2max_trend": [asdict(b) for b in vo2max],
                "scale_trend": {k: [asdict(b) for b in v] for k, v in scale.items()},
                "steps_avg_per_day_trend": [asdict(b) for b in steps_avg_per_day_trend],
                "readiness_trend": [asdict(p) for p in readiness_trend],
                "readiness_week": [asdict(p) for p in readiness_week],
                "acwr_trend": [asdict(p) for p in acwr_trend],
                "session_load_trend": [asdict(p) for p in session_load],
                "rpe_acwr_trend": [asdict(p) for p in rpe_acwr_trend],
                "session_calories_trend": [asdict(p) for p in session_calories],
                "session_trimp_trend": [asdict(p) for p in session_trimp],
                "monotony_trend": [asdict(p) for p in monotony_trend],
                "maintenance": asdict(maintenance) if maintenance else None,
                "current_week": exercise_analysis["current_week"],
                "stretch_current_week": exercise_analysis["stretch_current_week"],
                "overload_rows": exercise_analysis["overload_rows"],
            }
        ),
    )


@router.get("/settings/token", response_class=HTMLResponse)
def token_settings(request: Request):
    """Shows the token management page: what the shared secret is for,
    and a button to rotate it. The current secret is never rendered —
    after a rotation the new value is shown exactly once."""
    return templates.TemplateResponse(request, "token.html", {"new_token": None})


@router.post("/settings/token/rotate", response_class=HTMLResponse)
def rotate_token(request: Request):
    """Generates a new shared secret, activates it immediately and persists
    it (see app.security.rotate_shared_secret). The new value is rendered
    once so it can be copied into the Android app — afterwards the old
    token stops working."""
    new_token = security.rotate_shared_secret()
    return templates.TemplateResponse(request, "token.html", {"new_token": new_token})


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


# Rank per l'ordinamento dei box Volume+Frequenza: GREEN prima di YELLOW
# prima di RED; colori sconosciuti (es. GRAY) in coda. Vedi
# _current_week_sort_key sotto.
_SEMAPHORE_RANK = {"GREEN": 0, "YELLOW": 1, "RED": 2}


def _current_week_sort_key(box: dict) -> tuple:
    """Ordina i box Volume+Frequenza per semafori, non per nome.

    Tier 0: entrambi GREEN; tier 1: un solo GREEN (altro YELLOW prima di
    RED); tier 2: nessun GREEN (YELLOW+YELLOW, poi YELLOW+RED, poi RED+RED).
    A parita' di colori, ordine alfabetico per bodypart (comportamento
    precedente)."""
    vol_rank = _SEMAPHORE_RANK.get(box["volume_color"], 3)
    freq_rank = _SEMAPHORE_RANK.get(box["frequency_color"], 3)
    green_count = (box["volume_color"] == "GREEN") + (box["frequency_color"] == "GREEN")
    tier = 2 - green_count
    return (tier, tuple(sorted((vol_rank, freq_rank))), box["bodypart"])


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


def _exercise_analysis_context(conn, today: str | None) -> dict:
    """Current-week muscle-group volume/frequency (Schoenfeld-et-al.-derived
    semaphores) and progressive-overload verdicts for exercises trained this
    week — built on top of exercise_sets.bodypart. See app.analysis's module
    comments above muscle_group_weekly_trend and progressive_overload_verdict
    for the full reasoning/thresholds. "This week" is the calendar Monday..
    Sunday containing `today`, not a rolling 7-day window — same convention
    as exercise_ids_trained_this_week/muscle_group_weekly_trend's own
    Monday-keyed buckets. Shared by the dashboard's "Exercise analysis"
    section — this used to be its own /hypertrophy page, folded into Trends
    per user request."""
    muscle_trend = muscle_group_weekly_trend(conn, today=today)
    stretch_trend = stretch_weekly_trend(conn, today=today)
    trained_this_week = exercise_ids_trained_this_week(conn, today=today)
    exercises = list_exercises(conn)
    exercises_this_week = [e for e in exercises if e.exercise_id in trained_this_week]
    overload = [
        progressive_overload_verdict(conn, e.exercise_id, today=today) for e in exercises_this_week
    ]

    # Only the most recent week per bodypart, and only bodyparts actually
    # trained this week (set_count > 0) — per user decision, an empty card
    # for a muscle group not touched this week adds noise, not signal; the
    # historical trend for it is still available via muscle_trend if ever
    # needed, just not surfaced as a "this week" card. Ordinati per semafori
    # (vedi _current_week_sort_key), non per nome.
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
    current_week.sort(key=_current_week_sort_key)

    # Same "most recent week, only bodyparts actually stretched this week"
    # shape as current_week above. Minutes, not seconds, per user request —
    # rounded to one decimal so a 90 s hold reads as 1.5, not 2.
    stretch_current_week = [
        {
            "bodypart": bodypart,
            "minutes": round(weeks[-1].total_seconds / 60, 1),
            "volume_color": weeks[-1].volume_semaphore,
            "session_count": weeks[-1].session_count,
        }
        for bodypart, weeks in sorted(stretch_trend.items())
        if weeks and weeks[-1].total_seconds > 0
    ]

    exercise_names = {e.exercise_id: e.exercise_name or e.exercise_id for e in exercises}
    overload_rows = []
    for v in overload:
        e1rm_cell = _overload_cell(v.e1rm)
        tonnage_cell = _overload_cell(v.tonnage)
        # Skip exercises missing a verdict on either metric — per user
        # decision, a row with "(Not enough data)" in any column is noise in
        # a table meant to highlight movers, so it's omitted entirely.
        if e1rm_cell is None or tonnage_cell is None:
            continue
        overload_rows.append(
            {
                "exercise_id": v.exercise_id,
                "exercise_name": exercise_names.get(v.exercise_id, v.exercise_id),
                "e1rm": e1rm_cell,
                "tonnage": tonnage_cell,
                "_sort_key": v.e1rm.percent_per_week or v.tonnage.percent_per_week or 0,
            }
        )
    # Biggest movers (by e1RM rate, falling back to tonnage) surface first —
    # a single sort key since the table shows one row per exercise, not one
    # per metric; e1RM and tonnage verdicts can still disagree within a row.
    overload_rows.sort(key=lambda r: r["_sort_key"], reverse=True)

    return {
        "current_week": current_week,
        "stretch_current_week": stretch_current_week,
        "overload_rows": overload_rows,
    }


@router.get("/exercises", response_class=HTMLResponse)
def exercises_list(request: Request):
    with connect(DB_PATH) as conn:
        exercises = list_exercises(conn)

    return templates.TemplateResponse(request, "exercises.html", {"exercises": exercises})


@router.get("/exercises/{exercise_id}", response_class=HTMLResponse)
def exercise_detail(request: Request, exercise_id: str, today: str | None = Query(default=None)):
    with connect(DB_PATH) as conn:
        all_time = all_time_tonnage_pr(conn, exercise_id)
        # The three like-for-like PR variants in one round-trip instead of
        # three (same predicates and same pick logic — see the helper).
        prs = recent_prs_for_exercise(conn, exercise_id)
        recent = prs[(False, False)]
        recent_warmup = prs[(False, True)]
        recent_daily = prs[(True, False)]
        history = exercise_history(conn, exercise_id, today=today)
        bodyweight_load = latest_bodyweight_load(conn, exercise_id)
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
            "bodyweight_load": asdict(bodyweight_load) if bodyweight_load else None,
        },
    )
