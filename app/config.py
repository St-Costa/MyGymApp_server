"""Environment-driven configuration.

Kept as a single small module (no framework-specific settings class) since
there are only three knobs. Read once at import time — this process is
restarted, not reconfigured live, when env vars change.
"""

import os
from pathlib import Path

# The shared secret every request must present as `Authorization: Bearer <value>`.
# No default: an empty/missing secret would make auth meaningless, so we let
# startup fail loudly (see app/security.py) rather than silently accept anything.
SYNC_SHARED_SECRET = os.environ.get("SYNC_SHARED_SECRET", "")

# Root directory for all persistent server data: raw/, db.sqlite3, logs/.
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "./data")).resolve()

PORT = int(os.environ.get("PORT", "8420"))

RAW_DIR = DATA_ROOT / "raw"
DB_PATH = DATA_ROOT / "db.sqlite3"
LOGS_DIR = DATA_ROOT / "logs"

# Saved weekly report screenshots (app.screenshot.save_weekly_report_screenshot)
# — under DATA_ROOT rather than a hardcoded path so it persists through the
# same bind-mount as raw/ and db.sqlite3 (see Dockerfile), and accumulates
# across container restarts/redeploys.
REPORTS_DIR = DATA_ROOT / "reports"

# This app has no real stored birth date/profile field — used wherever an
# age-relative calculation is needed (RMSSD reference thresholds in
# dashboard.html, HR-zone max-HR estimation in app.ecg_analysis) as a
# stand-in so those calculations have *some* age-relative basis rather than
# none. Recomputed from the current date every time it's used (dashboard.html's
# own `currentAge`, ecg_analysis.py's zone calculation), so it drifts forward
# automatically as years pass rather than needing a yearly manual update —
# NOT this user's actual birth year. Replace with a real stored profile
# field if one is ever added.
ASSUMED_BIRTH_YEAR = 2000

# Same "no real stored profile field yet" situation as ASSUMED_BIRTH_YEAR
# above — height and biological sex are never synced from the phone (no
# session/readiness/scale field carries either), but are needed for the
# Mifflin-St Jeor BMR term of app.analysis.estimated_maintenance_calories.
# Hardcoded constants, not derived from anything else. Replace with a real
# stored profile field if one is ever added.
ASSUMED_HEIGHT_CM = 170
ASSUMED_SEX = "male"  # "male" or "female" — Mifflin-St Jeor has a sex-specific constant term.
