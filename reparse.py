#!/usr/bin/env python3
"""Standalone maintenance script: re-parse raw session files already on disk.

Never touches the network — reads DATA_ROOT/raw/**, re-runs the *same*
parsing function POST /v1/sessions uses (app.parsing.parse_session_file,
via app.ingestion — never duplicated here), and upserts the result exactly
as the live endpoint would. This is how the database catches up after the
parser is improved to handle a new field, without needing the phone
involved: the raw files already have everything they ever sent.

Usage:
    python reparse.py --all
    python reparse.py --stale
    python reparse.py <session_id> [<session_id> ...]
"""

import argparse
import logging
import sys
import traceback
from datetime import UTC, datetime

from app.config import DATA_ROOT, DB_PATH, RAW_DIR
from app.parsing import SessionParseError, parse_session_file
from app.storage import (
    connect,
    get_all_sessions,
    get_stale_sessions,
    init_db,
    record_unparsed_session,
    upsert_parsed_session,
)

logger = logging.getLogger("mygymapp.reparse")


def reparse_one(conn, session_id: str, raw_path: str) -> bool:
    """Re-reads RAW_DIR/<raw_path> and re-runs the shared parser. Returns
    True on a successful parse, False if it was recorded as a failure
    (never raises — a bad file must not abort a batch of many)."""
    full_path = RAW_DIR / raw_path
    if not full_path.exists():
        logger.error("raw file missing for session %s: %s", session_id, full_path)
        return False

    raw_bytes = full_path.read_bytes()
    received_at = datetime.now(UTC).isoformat()

    # content_hash isn't recomputed here — reparse re-derives the *parsed*
    # columns from bytes already accepted and hash-verified at ingest time;
    # it doesn't re-run step 3. The existing hash on the row is preserved by
    # SessionParseError not touching sessions.content_hash below.
    with conn:  # nested transaction scope per session; keeps one bad file isolated
        stored = conn.execute(
            "SELECT content_hash FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        content_hash = stored[0] if stored else ""

        try:
            parsed = parse_session_file(raw_bytes)
        except SessionParseError:
            logger.exception("failed to reparse session %s at %s", session_id, raw_path)
            record_unparsed_session(
                conn,
                session_id=session_id,
                content_hash=content_hash,
                raw_path=raw_path,
                received_at=received_at,
                error=traceback.format_exc(),
            )
            return False

        upsert_parsed_session(
            conn,
            parsed,
            content_hash=content_hash,
            raw_path=raw_path,
            received_at=received_at,
        )
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="reparse every stored session")
    group.add_argument(
        "--stale",
        action="store_true",
        help="reparse only sessions below the current schema_version",
    )
    group.add_argument(
        "session_ids", nargs="*", default=[], help="specific session id(s) to reparse"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    init_db(DB_PATH)

    with connect(DB_PATH) as conn:
        if args.all:
            targets = get_all_sessions(conn)
        elif args.stale:
            targets = get_stale_sessions(conn)
        else:
            placeholders = ",".join("?" * len(args.session_ids))
            rows = conn.execute(
                f"SELECT id, raw_path FROM sessions WHERE id IN ({placeholders})",
                args.session_ids,
            ).fetchall()
            targets = [(r[0], r[1]) for r in rows]

        if not targets:
            print("No matching sessions found.")
            return 0

        ok = 0
        failed = 0
        for session_id, raw_path in targets:
            if reparse_one(conn, session_id, raw_path):
                ok += 1
            else:
                failed += 1

    print(f"Reparsed {ok} session(s) successfully, {failed} failed (see logs).")
    return 1 if failed else 0


if __name__ == "__main__":
    if not DATA_ROOT.exists():
        print(f"DATA_ROOT does not exist: {DATA_ROOT}", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
