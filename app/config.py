"""Environment-driven configuration.

Kept as a single small module (no framework-specific settings class) since
there are only three knobs. Read once at import time — this process is
restarted, not reconfigured live, when env vars change.
"""

import os
from pathlib import Path

# Root directory for all persistent server data: raw/, db.sqlite3, logs/.
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "./data")).resolve()

# File holding a GUI-rotated secret (see app/security.rotate_shared_secret).
# It lives under DATA_ROOT so it survives container restarts (unlike the
# process env, which comes from the host .env via docker-compose and isn't
# writable from inside the container). When present and non-empty it wins
# over SYNC_SHARED_SECRET; otherwise the env var is the source of truth.
# Kept as a dotfile next to db.sqlite3/logs — never committed (see .gitignore).
SECRET_FILE = DATA_ROOT / ".sync_secret"


def _resolve_shared_secret() -> str:
    """The shared secret every request must present as
    `Authorization: Bearer <value>`. No default: an empty/missing secret
    would make auth meaningless, so callers fail loudly (see
    app/security.py) rather than silently accept anything."""
    try:
        file_secret = SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        file_secret = ""
    if file_secret:
        return file_secret
    return os.environ.get("SYNC_SHARED_SECRET", "")


SYNC_SHARED_SECRET = _resolve_shared_secret()

PORT = int(os.environ.get("PORT", "8420"))

# Optional off-box mirror for the data/raw/ backup git repo (docs/backup-
# server-brief.md Task 1.3): a bare repo on another disk, or a private
# GitHub/Gitea URL with a deploy key. When set, every successful commit is
# followed by a best-effort `git push <remote> HEAD:main` — a push failure
# is logged, never fatal (the local commit is the real backup). Empty/unset
# disables the mirror entirely.
BACKUP_GIT_REMOTE = os.environ.get("BACKUP_GIT_REMOTE", "")

RAW_DIR = DATA_ROOT / "raw"
DB_PATH = DATA_ROOT / "db.sqlite3"
LOGS_DIR = DATA_ROOT / "logs"

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
