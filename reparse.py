#!/usr/bin/env python3
"""Standalone maintenance script: re-parse raw session files already on disk.

Never touches the network — reads DATA_ROOT/raw/**, re-runs the *same*
parsing function POST /v1/sessions uses (do not duplicate that logic here),
and upserts the result exactly as the live endpoint would. This is how the
database catches up after the parser is improved to handle a new field,
without needing the phone involved.

Usage:
    python reparse.py --all
    python reparse.py --stale
    python reparse.py <session_id> [<session_id> ...]

Placeholder — implemented alongside the parsing logic per
docs/sync-ingestion/SPEC.md.
"""

if __name__ == "__main__":
    raise SystemExit("reparse.py not yet implemented — see docs/sync-ingestion/SPEC.md")
