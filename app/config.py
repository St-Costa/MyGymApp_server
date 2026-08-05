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
