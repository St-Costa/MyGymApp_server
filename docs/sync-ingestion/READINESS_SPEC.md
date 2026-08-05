# MyGymApp sync server — readiness events (addendum to SPEC.md)

## Context

This is an addition to the existing MyGymApp sync server (the one implementing
`POST /v1/sessions` per `docs/sync-ingestion/SPEC.md`). It adds a **second, independent**
endpoint for a different record type: HRV readiness measurements. Follow the same
raw-first, content-hash-idempotent design already used for sessions — this addendum only
covers what's different.

**One phone, one server, same auth model (shared bearer token) as sessions.**

## What a readiness event is

Once per Polar H10 connect, the phone runs a 60-second HRV measurement and computes a
readiness reading. Sent as a raw Markdown+YAML file, same format style as a session:

```markdown
---
id: a1b2c3d4
measuredAt: "2026-08-05T07:04:10.123"
readiness: "GOOD"
lnRmssd: 4.30
restingHr: 65
vo2max: 45.2
recommendation: "HRV above baseline. Good day to push intensity."
---
```

Field notes:
- `id`: 8-hex event ID (same ID scheme as everything else in this app — not globally
  unique across record types, only unique within its own type; a readiness event and a
  session could theoretically share the same 8-hex string by coincidence — keep readiness
  IDs in their own table/namespace, never conflate with `sessions.id`).
- `readiness`: one of `MEASURING | DELOAD_RECOMMENDED | LIGHT_DAY | NORMAL | GOOD | PEAK | NO_BASELINE`.
  In practice, only completed measurements are ever sent (never `MEASURING`, which is a
  transient in-progress UI state on the phone, never persisted or sent).
- `lnRmssd`, `restingHr`, `vo2max`: floats/int, always present (0/0.0 if genuinely
  unmeasured — same "reader defaults missing numeric fields to 0" tolerance as sessions).
- `recommendation`: free-text string, human-readable guidance already generated
  phone-side. Store as-is, no parsing needed.

## API

### `POST /v1/readiness`

Same shape as `POST /v1/sessions`, same auth (`Authorization: Bearer <shared-secret>`),
same request/response contract:

**Part `envelope`** (`application/json`):
```json
{
  "eventId": "a1b2c3d4",
  "relPath": "readiness/a1b2c3d4.md",
  "contentHash": "sha256:...",
  "appVersion": "1.0-...",
  "clientSentAt": "2026-08-05T07:04:12Z"
}
```
(Note: field is `eventId`, not `sessionId` — mirrors the phone's envelope for this
endpoint exactly.)

**Part `file`** (`text/markdown`): the raw bytes above.

### Handling — identical steps to `POST /v1/sessions`

1. Bearer auth check.
2. Envelope validation.
3. Hash verification against the `file` part's actual bytes → 422 on mismatch, nothing
   written.
4. Idempotency check on `eventId` (not `sessionId`) — same hash → `200 duplicate`,
   no-op. Different hash for existing `eventId` → treat as update (rare for readiness,
   since nothing on the phone currently rewrites a readiness file after creation, but
   handle it the same way for consistency and because "never" is not "impossible").
5. Write raw bytes to `<RAW_STORAGE_ROOT>/<relPath>` — i.e. `raw/readiness/{id}.md`,
   mirroring the phone's own `gymdata/readiness/{id}.md` layout.
6. Parse the YAML frontmatter (reuse the same tolerant parsing approach as the session
   parser — unknown fields ignored, missing numeric fields default to 0). On success,
   upsert into `readiness_events` (schema below). On failure, log it, keep the raw file,
   still respond `200 stored` — same "raw write and parse are different failure domains"
   principle as sessions.
7. Respond `200 OK`, `{"eventId": "...", "status": "stored"}` (or `"duplicate"`).

### `GET /health` — unchanged, already covers this endpoint too.

## SQLite schema addition

```sql
CREATE TABLE readiness_events (
  id             TEXT PRIMARY KEY,     -- 8-hex event id (own namespace, not sessions.id)
  measured_at    TEXT NOT NULL,
  readiness      TEXT,                 -- MEASURING|DELOAD_RECOMMENDED|LIGHT_DAY|NORMAL|GOOD|PEAK|NO_BASELINE
  ln_rmssd       REAL,
  resting_hr     INTEGER,
  vo2max         REAL,
  recommendation TEXT,
  content_hash   TEXT NOT NULL,
  raw_path       TEXT NOT NULL,
  received_at    TEXT NOT NULL,
  schema_version INTEGER NOT NULL
);
```

Independent table from `sessions` — a readiness event is not a child of any session (it
fires once per HR connect, whether or not a workout follows). Do not add a foreign key to
`sessions`.

Extend `reparse.py` (already built for sessions) to also accept readiness raw files and
re-run this parser — same shared-function principle: one parsing function per record type,
called from both the live endpoint and the batch reparse tool, never duplicated.

## Testing checklist (addition to the existing one)

1. `POST /v1/readiness` with a valid envelope + hand-written file matching the example
   above succeeds, writes to `raw/readiness/`, and a row appears in `readiness_events`.
2. Re-sending the same request → `duplicate`, no second row.
3. Bad hash → 422, nothing written.
4. Missing/wrong bearer token → 401 (should already work, same auth middleware as
   sessions — just confirm it also covers this new route).
5. A readiness `id` that happens to collide with an existing `sessions.id` does NOT cause
   any cross-table confusion (they're independent primary key spaces in independent
   tables — this should be true by construction, but worth a quick sanity check given both
   use the same 8-hex ID format).

## Non-goals (same as the session spec)

No weekly analysis logic here either — `readiness_events` is just another table the
(separate, future) analysis job can query. No new auth model, no public exposure beyond
what's already set up via Tailscale Serve for `/v1/sessions`.
