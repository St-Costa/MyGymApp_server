#!/usr/bin/env python3
"""Standalone maintenance script: re-parse raw record files already on disk,
across all four record types (sessions, readiness events, scale weigh-ins,
raw ECG recordings — see docs/sync-ingestion/{SPEC,READINESS_SPEC,
SCALE_SPEC,ECG_SPEC}.md).

Never touches the network — reads DATA_ROOT/raw/**, re-runs the *same*
per-type parsing function the live POST handlers use (app.parsing, via
app.ingestion — never duplicated here), and upserts the result exactly as
the live endpoint would. This is how the database catches up after a parser
is improved to handle a new field, without needing the phone involved: the
raw files already have everything they ever sent.

Usage:
    python reparse.py --all                  # every record, all four types
    python reparse.py --stale                # only schema_version < current, all types
    python reparse.py --type readiness --all  # restrict to one record type
    python reparse.py <id> [<id> ...]          # specific ids (searched across all types)
"""

import argparse
import logging
import sys
import traceback
from dataclasses import replace
from datetime import UTC, datetime

from app.config import DATA_ROOT, DB_PATH, RAW_DIR
from app.parsing import (
    SessionParseError,
    parse_ecg_file,
    parse_readiness_file,
    parse_scale_weighin_file,
    parse_session_file,
)
from app.storage import (
    connect,
    get_all_records,
    get_stale_records,
    id_column,
    init_db,
    record_parse_failure,
    upsert_parsed_ecg,
    upsert_parsed_readiness,
    upsert_parsed_scale_weighin,
    upsert_parsed_session,
)

logger = logging.getLogger("mygymapp.reparse")

# One entry per record type: (table name, parse function, upsert function).
# Adding a new record type means adding one line here, not a new code path —
# reparse_one/main below are already generic over this. `parse_ecg_file`
# doesn't recover an id from the bytes (see its docstring), so it's wrapped
# to fill in `record_id` the same way app.ingestion.ingest_ecg does.
RECORD_TYPES = {
    "sessions": ("sessions", parse_session_file, upsert_parsed_session),
    "readiness": ("readiness_events", parse_readiness_file, upsert_parsed_readiness),
    "scale": ("scale_weighins", parse_scale_weighin_file, upsert_parsed_scale_weighin),
    "ecg": (
        "ecg_recordings",
        lambda raw_bytes, record_id: replace(parse_ecg_file(raw_bytes), id=record_id),
        upsert_parsed_ecg,
    ),
}

# Whether a record type's parse function needs `record_id` passed alongside
# the raw bytes (only ecg — the other three parsers read `id` out of the
# YAML frontmatter itself).
_PARSE_FN_NEEDS_RECORD_ID = {"ecg"}


def reparse_one(conn, record_type: str, record_id: str, raw_path: str) -> bool:
    """Re-reads RAW_DIR/<raw_path> and re-runs the shared parser for
    `record_type`. Returns True on a successful parse, False if it was
    recorded as a failure (never raises — a bad file must not abort a batch
    of many)."""
    table, parse_fn, upsert_fn = RECORD_TYPES[record_type]

    full_path = RAW_DIR / raw_path
    if not full_path.exists():
        logger.error("raw file missing for %s %s: %s", record_type, record_id, full_path)
        return False

    raw_bytes = full_path.read_bytes()
    received_at = datetime.now(UTC).isoformat()

    # content_hash isn't recomputed here — reparse re-derives the *parsed*
    # columns from bytes already accepted and hash-verified at ingest time;
    # it doesn't re-run step 3. The existing hash on the row is preserved by
    # SessionParseError not touching it below.
    with conn:  # nested transaction scope per record; keeps one bad file isolated
        stored = conn.execute(
            f"SELECT content_hash FROM {table} WHERE {id_column(table)} = ?",  # noqa: S608
            (record_id,),
        ).fetchone()
        content_hash = stored[0] if stored else ""

        try:
            if record_type in _PARSE_FN_NEEDS_RECORD_ID:
                parsed = parse_fn(raw_bytes, record_id)
            else:
                parsed = parse_fn(raw_bytes)
        except SessionParseError:
            logger.exception("failed to reparse %s %s at %s", record_type, record_id, raw_path)
            record_parse_failure(
                conn,
                table=table,
                record_type=table,
                record_id=record_id,
                content_hash=content_hash,
                raw_path=raw_path,
                received_at=received_at,
                error=traceback.format_exc(),
            )
            return False

        upsert_fn(
            conn,
            parsed,
            content_hash=content_hash,
            raw_path=raw_path,
            received_at=received_at,
        )
        return True


def _targets_for_type(
    conn, record_type: str, *, all_records: bool, stale: bool
) -> list[tuple[str, str]]:
    table = RECORD_TYPES[record_type][0]
    if all_records:
        return get_all_records(conn, table=table)
    if stale:
        return get_stale_records(conn, table=table)
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--type",
        choices=list(RECORD_TYPES),
        default=None,
        help="restrict to one record type (default: all three)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="reparse every stored record")
    group.add_argument(
        "--stale",
        action="store_true",
        help="reparse only records below the current schema_version",
    )
    group.add_argument("record_ids", nargs="*", default=[], help="specific record id(s) to reparse")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    init_db(DB_PATH)

    types_to_scan = [args.type] if args.type else list(RECORD_TYPES)

    with connect(DB_PATH) as conn:
        targets: list[tuple[str, str, str]] = []  # (record_type, id, raw_path)

        if args.all or args.stale:
            for record_type in types_to_scan:
                for record_id, raw_path in _targets_for_type(
                    conn, record_type, all_records=args.all, stale=args.stale
                ):
                    targets.append((record_type, record_id, raw_path))
        else:
            # Specific ids: search each requested type's table (or all three
            # if --type wasn't given) since ids are only unique within their
            # own record type, not globally.
            for record_type in types_to_scan:
                table = RECORD_TYPES[record_type][0]
                col = id_column(table)
                placeholders = ",".join("?" * len(args.record_ids))
                rows = conn.execute(
                    f"SELECT {col}, raw_path FROM {table} WHERE {col} IN ({placeholders})",  # noqa: S608
                    args.record_ids,
                ).fetchall()
                targets.extend((record_type, r[0], r[1]) for r in rows)

        if not targets:
            print("No matching records found.")
            return 0

        ok = 0
        failed = 0
        for record_type, record_id, raw_path in targets:
            if reparse_one(conn, record_type, record_id, raw_path):
                ok += 1
            else:
                failed += 1

    print(f"Reparsed {ok} record(s) successfully, {failed} failed (see logs).")
    return 1 if failed else 0


if __name__ == "__main__":
    if not DATA_ROOT.exists():
        print(f"DATA_ROOT does not exist: {DATA_ROOT}", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
