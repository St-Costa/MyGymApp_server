"""In-process cron for the weekly report screenshot (app.screenshot).

Runs inside the same long-lived server process (docker-compose.yaml has
`restart: unless-stopped`) rather than depending on an external cron job —
generate_report.py's own docstring notes it was written for a cron job that
was never actually set up. The day-of-week/hour are user-configurable from
the GUI (GET/POST /weekly-report/schedule in app.gui) and persisted in
app_settings so they survive a restart; this module only owns the running
APScheduler instance and (re)building its single job from that config.

Single job, replace_existing=True: reconfiguring always replaces the same
job id rather than accumulating one per save. Errors from the job itself
(e.g. Playwright/Chromium failing) are logged, not raised — a missed/failed
screenshot must never crash the server process (same raw-first spirit as
the rest of the app: a best-effort side task failing silently is fine, but
must not take anything else down with it).
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import DB_PATH
from app.storage import connect, get_setting, set_setting

logger = logging.getLogger(__name__)

# APScheduler's cron trigger numbers Monday=0..Sunday=6 (day_of_week as int),
# but also accepts these three-letter names directly — used as-is so the
# stored setting value and the trigger argument are the same string, no
# int<->name mapping to keep in sync.
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_LABELS = {
    "mon": "Lunedì",
    "tue": "Martedì",
    "wed": "Mercoledì",
    "thu": "Giovedì",
    "fri": "Venerdì",
    "sat": "Sabato",
    "sun": "Domenica",
}

_TZ = "Europe/Rome"
_JOB_ID = "weekly_report_screenshot"
_SETTING_DAY = "weekly_report_schedule_day"
_SETTING_HOUR = "weekly_report_schedule_hour"

# Defaults if nothing has been saved yet: Sunday 06:00 — matches this
# feature's original request, not an arbitrary placeholder.
DEFAULT_DAY = "sun"
DEFAULT_HOUR = 6

_scheduler: BackgroundScheduler | None = None


def get_schedule() -> tuple[str, int]:
    """Reads the configured (day, hour) from app_settings, falling back to
    the defaults above when nothing has been saved yet."""
    with connect(DB_PATH) as conn:
        day = get_setting(conn, _SETTING_DAY) or DEFAULT_DAY
        hour_raw = get_setting(conn, _SETTING_HOUR)
    return day, int(hour_raw) if hour_raw is not None else DEFAULT_HOUR


def set_schedule(day: str, hour: int) -> None:
    """Validates and persists (day, hour), then reschedules the running job
    to match — the GUI's POST handler calls this directly rather than
    writing settings itself, so "saved" and "actually scheduled" can never
    drift apart."""
    if day not in WEEKDAYS:
        raise ValueError(f"invalid day: {day!r} (expected one of {WEEKDAYS})")
    if not 0 <= hour <= 23:
        raise ValueError(f"invalid hour: {hour!r} (expected 0-23)")

    with connect(DB_PATH) as conn:
        set_setting(conn, _SETTING_DAY, day)
        set_setting(conn, _SETTING_HOUR, str(hour))

    if _scheduler is not None:
        _reschedule(_scheduler, day, hour)


def _run_weekly_report_job() -> None:
    # Imported lazily: app.screenshot pulls in Playwright, which this module
    # otherwise wouldn't need at import time (mirrors app.gui's own lazy
    # import of app.screenshot for the same reason).
    from app.screenshot import save_weekly_report_screenshot

    try:
        saved_path = save_weekly_report_screenshot()
        logger.info("Scheduled weekly report screenshot saved: %s", saved_path)
    except Exception:
        logger.exception("Scheduled weekly report screenshot failed")


def _reschedule(scheduler: BackgroundScheduler, day: str, hour: int) -> None:
    scheduler.add_job(
        _run_weekly_report_job,
        trigger=CronTrigger(day_of_week=day, hour=hour, minute=0, timezone=_TZ),
        id=_JOB_ID,
        replace_existing=True,
    )


def start() -> BackgroundScheduler:
    """Starts the background scheduler and schedules the current (or
    default) config. Called once from app.main's lifespan; idempotent
    within one process since it always replaces the same job id."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=_TZ)
        _scheduler.start()
    day, hour = get_schedule()
    _reschedule(_scheduler, day, hour)
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
