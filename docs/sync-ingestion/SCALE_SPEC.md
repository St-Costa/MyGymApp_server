# MyGymApp sync server — scale weigh-ins (addendum to SPEC.md)

## Context

Second addendum to the MyGymApp sync server (alongside `READINESS_SPEC.md`). Adds a
**third, independent** endpoint: VitaFit VT701 smart-scale weigh-ins (weight, BMI, body
fat %, lean mass %). Same raw-first, content-hash-idempotent design as the other two
endpoints — this addendum only covers what's different.

**One phone, one server, same auth model (shared bearer token) as sessions/readiness.**

## What a scale weigh-in is

Once per day (when the user steps on the scale), the phone records weight + a
bioimpedance-derived body composition estimate. Sent as a raw Markdown+YAML file:

```markdown
---
id: "2026-08-05"
date: "2026-08-05"
recordedAt: "2026-08-05T07:12:34"
weightKg: 78.4
bmi: 24.1
bodyFatPercent: 16.8
leanMassPercent: 81.2
---
```

**Important field note — read before writing the parser**: `id` is an **ISO date string**
(`"2026-08-05"`), not the 8-hex UUID format used by sessions and readiness events. This is
because the phone stores at most **one weigh-in per calendar day** — weighing in again the
same day overwrites the existing file rather than creating a new one. Consequences for
this server:

- `id` doubles as the natural idempotency/update key, same role `sessionId`/`eventId` play
  for the other two endpoints — no special-casing needed, the existing "same id + same
  hash → duplicate; same id + different hash → update" rule applies unchanged.
- A same-day re-weigh is expected to arrive as an **update** (new hash, same id) more often
  than the other record types, since the phone always re-sends after any local overwrite.
  This is normal, not an error condition.
- Do not assume `id` is globally unique across record types — a session or readiness event
  could theoretically have an id string that collides with a date string, though in
  practice never will (different formats). Keep `scale_weighins.id` in its own table/
  namespace regardless, same as `readiness_events.id`.

## API

### `POST /v1/scale-weighins`

Same shape, same auth, same response contract as the other two endpoints:

**Part `envelope`** (`application/json`):
```json
{
  "weighInId": "2026-08-05",
  "relPath": "2026/08/2026-08-05.md",
  "contentHash": "sha256:...",
  "appVersion": "1.0-...",
  "clientSentAt": "2026-08-05T07:12:36Z"
}
```
(Field is `weighInId`, matching the phone's envelope for this endpoint.)

**Part `file`** (`text/markdown`): the raw bytes above.

### Handling — identical steps to the other two endpoints

1. Bearer auth check.
2. Envelope validation.
3. Hash verification → 422 on mismatch, nothing written.
4. Idempotency check on `weighInId`. Same hash → `200 duplicate`. Different hash for a
   known `weighInId` → update (this will happen more often here than for sessions/
   readiness, per the note above — that's expected, not a bug).
5. Write raw bytes to `<RAW_STORAGE_ROOT>/<relPath>` — i.e. `raw/scale/2026/08/2026-08-05.md`,
   mirroring the phone's own `gymdata/scale/YYYY/MM/{date}.md` layout.
6. Parse the YAML frontmatter (same tolerant approach as the other two parsers). On
   success, upsert into `scale_weighins` (schema below). On failure, log it, keep the raw
   file, still respond `200 stored`.
7. Respond `200 OK`, `{"weighInId": "...", "status": "stored"}` (or `"duplicate"`/
   `"updated"`, your choice on whether to distinguish).

### `GET /health` — unchanged, already covers this endpoint too.

## SQLite schema addition

```sql
CREATE TABLE scale_weighins (
  id                TEXT PRIMARY KEY,   -- ISO date string, e.g. "2026-08-05"
  date              TEXT NOT NULL,      -- same value as id, kept as its own column for
                                         -- query consistency with sessions.date
  recorded_at       TEXT,
  weight_kg         REAL,
  bmi               REAL,
  body_fat_percent  REAL,
  lean_mass_percent REAL,
  content_hash      TEXT NOT NULL,
  raw_path          TEXT NOT NULL,
  received_at       TEXT NOT NULL,
  schema_version    INTEGER NOT NULL
);
```

Independent table, no foreign key to `sessions` or `readiness_events` — a weigh-in isn't
tied to a workout or a specific HR connect.

Extend `reparse.py` to also handle scale weigh-in raw files, same shared-parsing-function
principle already established for sessions and readiness.

## Testing checklist (addition to the existing ones)

1. `POST /v1/scale-weighins` with a valid envelope + hand-written file matching the example
   above succeeds, writes to `raw/scale/`, and a row appears in `scale_weighins`.
2. Re-sending the same request → `duplicate`, no second row.
3. Sending an updated weigh-in for the **same date** (different weight/hash) → the
   existing row is overwritten, not duplicated. This is the main behavior worth testing
   carefully here, since it's expected to happen routinely (unlike sessions/readiness,
   where a same-id resend is normally just a retry, not a real content change).
4. Bad hash → 422, nothing written.
5. Missing/wrong bearer token → 401.

## Non-goals (same as the other two specs)

No weekly analysis logic here either. No new auth model, no public exposure beyond what's
already set up via Tailscale Serve.
