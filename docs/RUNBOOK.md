# Runbook

Operational playbook for MyGymApp Sync Server running on the self-hosted box.

## Overview

- **What this service does in one sentence**: Receives workout session files pushed by the
  MyGymApp Android app, stores the raw bytes durably, and parses them into a queryable
  SQLite database.
- **Where it runs**: A self-hosted machine, reachable only via Tailscale (`tailscale serve`,
  not Funnel). See [docs/sync-ingestion/SPEC.md](sync-ingestion/SPEC.md) for the full setup.
- **Who's on call / how to reach them**: Single-operator personal project — no on-call rotation.

## Health checks

- **How to tell it's healthy**: `GET /health` (no auth) returns `200`. Check locally
  (`curl http://localhost:$PORT/health`) or from another tailnet device via the
  `https://<machine>.<tailnet>.ts.net/health` hostname.
- **Dashboards**: none yet — this is a single-user personal service.
- **Logs**: `DATA_ROOT/logs/` (see `.env` for `DATA_ROOT`). Parse failures are logged with
  the `sessionId` and raw file path so `reparse.py` can retry them later.

## Deploying

- **How a deploy happens**: build and run the Docker image (`make build`), bind-mounting
  `DATA_ROOT` and passing `SYNC_SHARED_SECRET`/`PORT` as env vars. Publish the container port
  to `127.0.0.1` only — never `0.0.0.0` on the host — since `tailscale serve` reverse-proxies
  to localhost.
- **How to roll back**: re-run the previous image tag; the raw file store and SQLite DB are
  untouched by a code rollback since they live in the bind-mounted `DATA_ROOT`.
- **Pre-deploy checklist**: if the parser changed, bump `PARSER_SCHEMA_VERSION`
  (`app/storage.py`) and plan a `python reparse.py --stale` (or `--all`) right after deploy;
  if the ECG analysis math changed, bump `ECG_ANALYSIS_SCHEMA_VERSION` and plan a
  `python analyze_ecg.py --stale`. Schema changes need no migrations framework — additive
  `ALTER TABLE` migrations run automatically at startup (see
  [docs/sync-ingestion/SPEC.md](sync-ingestion/SPEC.md#schema_version-and-the-reparse-tool)).

## Common incidents

### Parser can't handle a session file

- **Symptom**: A session row exists with `schema_version = 0` (or in a `parse_failures`
  table), meaning the raw file was stored but parsing failed.
- **Likely cause(s)**: The Android app added/changed a YAML field the parser doesn't know
  about yet.
- **Diagnosis steps**: Check `DATA_ROOT/logs/` for the exception and `sessionId`; inspect the
  raw file at `DATA_ROOT/raw/<relPath>`.
- **Fix**: Update the parser to handle the new shape, bump `PARSER_SCHEMA_VERSION`, then run
  `python reparse.py --stale` (or `--all`) to catch up every affected row. No data was lost —
  the raw file was always there.
- **Follow-up**: none required; this is expected, routine maintenance per the raw-first design.

### ECG recordings stuck with `analyzed_at IS NULL`

- **Symptom**: rows in `ecg_recordings` with `analyzed_at IS NULL` — the waveform stored fine
  but the analysis never landed (check: `SELECT session_id FROM ecg_recordings WHERE
  analyzed_at IS NULL`).
- **Likely cause(s)**: a bug/edge case in the analysis math (logged, never fatal to the
  upload), or recordings that landed before automatic analysis was wired in.
- **Diagnosis steps**: check `DATA_ROOT/logs/` for `ECG analysis failed`; the raw file at
  `DATA_ROOT/raw/<relPath>` is intact.
- **Fix**: `python analyze_ecg.py --stale` (or `--all`, or specific session ids). Repeatable
  any time — this is the designed retry path, not a workaround.

### Repo exercises/routines rows missing after a restart

- **Symptom**: `POST /v1/repo` returned 2xx but the `exercises`/`routines` SQL view has no
  matching row.
- **Likely cause(s)**: the repo parse worker's queue is in-memory — a restart drops whatever
  hadn't been parsed yet (the raw file is safe; see
  [docs/backup/OPTIMIZATION.md](backup/OPTIMIZATION.md) point 6).
- **Fix**: `python reparse.py --type exercises --stale` and `--type routines --stale` (or
  `--all`). On a clean shutdown `stop_worker()` drains the queue first, so this only bites
  on crashes.

### Bulk request rejected with 413

- **Symptom**: `POST /v1/repo/bulk` returns `413`, or `POST /v1/repo/files` rejects `relPaths`.
- **Likely cause(s)**: the batch exceeded `BULK_MAX_FILES` (500) entries or
  `BULK_MAX_TOTAL_BYTES` (50 MB) — these are server-side soft caps, not a bug.
- **Fix**: client-side — split into smaller batches. No server action needed.

### Debugging notes (not incidents)

- **Git log lags writes by ~3s**: commits are debounced, so a just-synced file won't appear
  in `git log` until the burst settles. A 2xx means "fsynced", not "committed" — check the
  working tree first, `git log` second.
- **`raw/deleted/routines/_debug-backup-*.md` tombstones are expected**: one per press of the
  phone's "Test backup verso il server" button (an upsert immediately followed by a delete).
  Harmless — trim manually if they accumulate.
- **Error-code cheat sheet**: `401` bad/missing bearer; `400` malformed envelope JSON or bad
  `sleepQuality`; `422` hash mismatch or unsafe `relPath`; `404` restore of a non-live file;
  `413` bulk caps (above); `500` only when the raw write itself failed (disk full/permissions).

## Escalation

- **When to page someone vs. handle it yourself**: single-operator project — always handle
  it yourself.
- **Who owns which area**: see [.github/CODEOWNERS](../.github/CODEOWNERS).

## Disaster recovery

- **Backup location and restore procedure**: back up `DATA_ROOT` (raw files + `db.sqlite3`)
  by whatever means you back up the rest of the self-hosted box. If `db.sqlite3` is lost or
  corrupted but `DATA_ROOT/raw/` survives, delete the DB file and run `python reparse.py --all`
  to rebuild it entirely from the raw files.
- **`DATA_ROOT/raw/` is itself a git repo** ([docs/backup/README.md](backup/README.md)):
  commits are debounced (one per burst of writes, ~3s after it settles), so a hard crash can
  lose the *commit* for the last few seconds of writes — the raw files are still on the
  working tree and the next startup's `git add -A` sweeps them in. To recover a specific
  file: `git -C "$DATA_ROOT/raw" log --oneline -- <relPath>` then
  `git -C "$DATA_ROOT/raw" show <commit>:<relPath>`, or look in `raw/deleted/`. Set
  `BACKUP_GIT_REMOTE` for an automatic off-box mirror push after each commit.
- **RTO / RPO targets**: informal — personal project. Losing `db.sqlite3` is fully recoverable
  from raw files; losing `DATA_ROOT/raw/` itself loses the source data (protect this directory
  with regular backups).
- **`DATA_ROOT/raw/ecg/` needs extra care**: for sessions/readiness/scale, the phone keeps a
  permanent local copy, so a lost raw file can always be re-synced by asking the phone. ECG is
  different — the phone deletes its local `.ecg` file once this server confirms `stored`, and
  unconditionally after 30 days pending either way (see
  [ECG_SPEC.md](sync-ingestion/ECG_SPEC.md#phone-side-retention)). Once accepted here, the
  server's copy is the *only* copy — losing `raw/ecg/` loses that waveform permanently, not
  just until the next sync. Back this directory up with at least the same regularity as the
  rest of `raw/`, and treat it as the higher-priority half of that backup if you ever have to
  choose.
