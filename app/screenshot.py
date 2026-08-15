"""Full-page screenshot of the dashboard (app.gui's `/`), for validating the
"send an image instead of text" idea on the weekly-report preview page
(app/templates/weekly_report.html) before any real ntfy send path exists.

Playwright drives a real Chromium over loopback HTTP rather than rendering
the Jinja2 template in-process — this is deliberate: the goal is a faithful
capture of what a user actually sees in a browser (client-side chart JS
included), not a re-render of the raw HTML. That means this only works
against a server that's already listening on `app.config.PORT`; it cannot
screenshot itself via ASGI transport.

Dev-only for now (see pyproject.toml's `playwright` dependency comment) —
not used by any production send path yet.
"""

from playwright.sync_api import sync_playwright

from app.config import PORT

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
