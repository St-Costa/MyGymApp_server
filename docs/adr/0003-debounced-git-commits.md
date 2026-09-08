# ADR 0003: Debounced git commits for `data/raw/`

- Status: accepted
- Date: 2026-09-04

## Context

The original backup design committed `data/raw/` inline in every ingest handler
(`git add -A && git commit` per request). A backfill of dozens of files meant dozens of
`git` forks and tree writes on the request path, and every bulk operation scaled with N.

## Decision

Handlers call `request_commit(...)` after the raw bytes are fsynced and return immediately;
a background committer (`app/git_backup.py`) folds the burst into a single commit after
`COMMIT_DEBOUNCE_SECONDS` (~3s) of write silence. The 2xx contract changed accordingly:
it means "raw bytes fsynced + hash verified", NOT "committed". A crash between fsync and
commit leaves files uncommitted on the working tree; the next burst's commit (or
`ensure_repo`/`flush` on restart) sweeps them in — nothing is lost because the raw files
are the source of truth. A `git commit` failure is logged, never a 500.

## Alternatives considered

- **One commit per request**: simple and synchronous, but N forks per backfill and git
  latency on the phone-visible request path.
- **Async queue with per-file commits**: still N commits, just off-path; keeps the cost,
  loses the burst grouping that makes history readable.

## Consequences

Backfills land as one commit per burst with a `sync … — sessions:N …` message; `git log`
lags writes by ~3s (see the RUNBOOK debugging notes). Don't change the debounce/flush
model, the commit-message format, or the soft-delete-to-`deleted/` behaviour without a
reason — see [docs/backup/OPTIMIZATION.md](../backup/OPTIMIZATION.md).
