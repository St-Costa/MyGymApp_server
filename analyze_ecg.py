#!/usr/bin/env python3
"""Standalone maintenance script: runs the docs/sync-ingestion/
ECG_ANALYSIS_HANDOFF.md analysis (§1-§5 — R-peak/HRV/arrhythmia metrics)
against raw ECG recordings already on disk.

Never touches the network — reads DATA_ROOT/raw/ecg/**, re-runs the *same*
analyze_ecg_recording function app.ingestion.ingest_ecg calls automatically
on every new upload (never duplicated here), and upserts the result exactly
as the live endpoint would. This is the queue-draining counterpart to that
in-request analysis: it exists for (a) the backlog of recordings that landed
before automatic analysis was wired in, and (b) re-analysis after a formula/
threshold change bumps ECG_ANALYSIS_SCHEMA_VERSION.

Separate from reparse.py rather than folded into it: reparse.py re-derives
header fields (sample_rate/duration) from ecg_recordings' own parser, an
entirely different concern from re-running waveform analysis — the two can
be stale independently of each other (see app.storage's two separate
schema_version columns), so keeping them as separate scripts avoids one
script's flags/semantics leaking into the other's.

Usage:
    python analyze_ecg.py --all      # every recording never analyzed, ever
    python analyze_ecg.py --stale    # only ecg_analysis.schema_version < current
    python analyze_ecg.py <session_id> [<session_id> ...]   # specific ids, force re-run
"""

import argparse
import logging
import sys
from datetime import UTC, datetime

from app.config import DATA_ROOT, DB_PATH, RAW_DIR
from app.ecg_analysis import analyze_ecg_recording
from app.parsing import SessionParseError, parse_ecg_file, read_ecg_samples
from app.storage import (
    connect,
    get_stale_ecg_analysis,
    get_unanalyzed_ecg_recordings,
    init_db,
    upsert_ecg_analysis,
)

logger = logging.getLogger("mygymapp.analyze_ecg")


def analyze_one(conn, session_id: str, raw_path: str) -> bool:
    """Re-reads RAW_DIR/<raw_path> and re-runs analyze_ecg_recording for one
    session. Returns True on success, False if the recording couldn't even
    be read (missing file, unparseable header) — never raises, so one bad
    file can't abort a batch of many."""
    full_path = RAW_DIR / raw_path
    if not full_path.exists():
        logger.error("raw ECG file missing for %s: %s", session_id, full_path)
        return False

    raw_bytes = full_path.read_bytes()

    try:
        parsed = parse_ecg_file(raw_bytes)
        samples = read_ecg_samples(raw_bytes)
    except SessionParseError:
        logger.exception("failed to parse ECG header for %s at %s", session_id, raw_path)
        return False

    result = analyze_ecg_recording(
        session_id, samples, parsed.sample_rate, file_size_bytes=len(raw_bytes)
    )

    analyzed_at = datetime.now(UTC).isoformat()
    with conn:  # nested transaction scope per recording; keeps one bad file isolated
        upsert_ecg_analysis(conn, result, analyzed_at=analyzed_at)

    if result.avg_hr is None:
        logger.info(
            "%s: below minimum-input thresholds — no result (peak_count=%d)",
            session_id,
            result.peak_count,
        )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="analyze every never-analyzed recording")
    group.add_argument(
        "--stale",
        action="store_true",
        help="re-analyze only recordings below the current ECG_ANALYSIS_SCHEMA_VERSION",
    )
    group.add_argument(
        "session_ids", nargs="*", default=[], help="specific session id(s) to (re-)analyze"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    init_db(DB_PATH)

    with connect(DB_PATH) as conn:
        if args.all:
            targets = get_unanalyzed_ecg_recordings(conn)
        elif args.stale:
            targets = get_stale_ecg_analysis(conn)
        else:
            placeholders = ",".join("?" * len(args.session_ids))
            rows = conn.execute(
                f"SELECT session_id, raw_path FROM ecg_recordings "  # noqa: S608
                f"WHERE session_id IN ({placeholders})",
                args.session_ids,
            ).fetchall()
            targets = [(r[0], r[1]) for r in rows]

        if not targets:
            print("No matching recordings found.")
            return 0

        ok = 0
        failed = 0
        for session_id, raw_path in targets:
            if analyze_one(conn, session_id, raw_path):
                ok += 1
            else:
                failed += 1

    print(f"Analyzed {ok} recording(s) successfully, {failed} failed (see logs).")
    return 1 if failed else 0


if __name__ == "__main__":
    if not DATA_ROOT.exists():
        print(f"DATA_ROOT does not exist: {DATA_ROOT}", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
