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
- **Pre-deploy checklist**: none currently — no migrations framework; schema changes are
  handled by bumping `PARSER_SCHEMA_VERSION` and reparsing (see
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

## Escalation

- **When to page someone vs. handle it yourself**: single-operator project — always handle
  it yourself.
- **Who owns which area**: see [.github/CODEOWNERS](../.github/CODEOWNERS).

## Disaster recovery

- **Backup location and restore procedure**: back up `DATA_ROOT` (raw files + `db.sqlite3`)
  by whatever means you back up the rest of the self-hosted box. If `db.sqlite3` is lost or
  corrupted but `DATA_ROOT/raw/` survives, delete the DB file and run `python reparse.py --all`
  to rebuild it entirely from the raw files.
- **RTO / RPO targets**: informal — personal project. Losing `db.sqlite3` is fully recoverable
  from raw files; losing `DATA_ROOT/raw/` itself loses the source data (protect this directory
  with regular backups).
