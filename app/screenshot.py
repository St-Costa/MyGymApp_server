"""Full-page screenshot of the dashboard (app.gui's `/`) — both the "send an
image instead of text" experiment on the weekly-report preview page
(app/templates/weekly_report.html), and the on-disk save consumed by
generate_report.py for the future ntfy/cron send path.

Playwright drives a real Chromium over loopback HTTP rather than rendering
the Jinja2 template in-process — this is deliberate: the goal is a faithful
capture of what a user actually sees in a browser (client-side chart JS
included), not a re-render of the raw HTML. That means this only works
against a server that's already listening on `app.config.PORT`; it cannot
screenshot itself via ASGI transport.
"""

from datetime import date as date_cls
from pathlib import Path

from playwright.sync_api import sync_playwright

from app.config import PORT, REPORTS_DIR

# Wide enough that the dashboard's multi-column cards lay out the same way
# they do on a desktop browser; height is irrelevant since full_page=True
# grows the viewport to the page's actual content height before capturing.
_VIEWPORT = {"width": 1280, "height": 1024}


def screenshot_dashboard_png(*, today: str | None = None) -> bytes:
    """Renders app.gui's `/` in headless Chromium and returns a full-page
    PNG. `today` is forwarded as the same `?today=` query param the
    dashboard route accepts, for reproducible screenshots in tests."""
    url = f"http://127.0.0.1:{PORT}/"
    if today:
        url += f"?today={today}"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport=_VIEWPORT)
            page.goto(url, wait_until="networkidle")
            return page.screenshot(full_page=True)
        finally:
            browser.close()


def save_weekly_report_screenshot(*, today: str | None = None) -> Path:
    """Screenshots the dashboard and saves it under REPORTS_DIR as
    `report_YYYYMMDD.png`, named for the day this ran (not `today`, which
    only controls what the dashboard renders — see app.gui's `?today=`).
    Creates REPORTS_DIR if it doesn't exist yet. Never deletes older
    reports — they're meant to accumulate, one per generation, as a manual
    archive independent of the SQLite layer. Returns the saved path.

    A same-day re-run overwrites that day's file rather than erroring or
    suffixing — running this twice in one day is a re-generation of the
    same day's report, not two different reports."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    png_bytes = screenshot_dashboard_png(today=today)

    filename = f"report_{date_cls.today():%Y%m%d}.png"
    out_path = REPORTS_DIR / filename
    out_path.write_bytes(png_bytes)
    return out_path
