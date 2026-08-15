#!/usr/bin/env python3
"""Standalone script: forces generation of the weekly report screenshot and
saves it under DATA_ROOT/reports/ (see app.screenshot and app.config's
REPORTS_DIR) as `report_YYYYMMDD.png`, named for the day this runs.

Never deletes older reports — they accumulate, one file per generation.
Intended for (a) manual/test runs and (b) an external cron job hitting this
directly (or POST /weekly-report/generate, the HTTP equivalent — see
app.gui) on a weekly schedule once one is set up; this script has no
scheduling of its own.

Unlike reparse.py/analyze_ecg.py, this needs the app server itself already
running and reachable at http://127.0.0.1:PORT — app.screenshot drives a
real browser against the live dashboard route, not an in-process render.

Usage:
    python generate_report.py                  # today's report
    python generate_report.py --today 2026-08-09  # dashboard as of a past date
"""

import argparse
import sys

from app.config import DATA_ROOT
from app.screenshot import save_weekly_report_screenshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--today",
        default=None,
        help="ISO date forwarded to the dashboard's own ?today= param "
        "(what the dashboard renders as 'today'); defaults to the real "
        "current date, same as not passing it to the dashboard at all.",
    )
    args = parser.parse_args()

    saved_path = save_weekly_report_screenshot(today=args.today)
    print(f"Saved {saved_path}")
    return 0


if __name__ == "__main__":
    if not DATA_ROOT.exists():
        print(f"DATA_ROOT does not exist: {DATA_ROOT}", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
