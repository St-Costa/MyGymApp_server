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
stepsAvgPerDay: 8214.5
stepsDaysSpanned: 1
sleepQuality: 4
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
- `stepsAvgPerDay` / `stepsDaysSpanned` (SYNC.md "Daily step average", phone-side
  spec): daily step average and how many days it was averaged over. Both nullable —
  `null` means "no data available" (no reading yet, sensor absent, permission
  denied, phone rebooted between readings), **not** zero steps. Never coerce
  `null` to 0 in the DB — a day with no step reading must stay indistinguishable
  from "unmeasured", not collapse into "measured as fully sedentary", or aggregate
  stats/graphs over this column are silently wrong. `stepsDaysSpanned` lets
  downstream analysis tell a clean same-day reading from one smeared across
  several skipped days, without discarding the value outright.
- `sleepQuality`: subjective 1..5 sleep rating the user taps on a faccina after the
  reading. Nullable — `null` (JSON literal, never an omitted key) means "non
  valutato" (the user hasn't rated it). Never coerce `null` to 0. Unlike the two
  steps fields, a present-but-malformed envelope value (not `null`, not an integer
  in `[1,5]`) is a hard **400**, nothing written — a subjective scale where every
  value carries meaning can't absorb a silent coercion. The `.md` frontmatter
  carries a redundant copy (`sleepQuality: 4`) for `reparse.py`; an out-of-range
  value there degrades to `null` rather than raising, keeping the raw-first
  guarantee. The user can rate sleep *after* the measurement already synced — see
  the idempotency note in step 4 below, this is the main reason a readiness file
  gets rewritten and re-sent.

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
  "clientSentAt": "2026-08-05T07:04:12Z",
  "stepsAvgPerDay": 8214.5,
  "stepsDaysSpanned": 1,
  "sleepQuality": 4
}
```
(Note: field is `eventId`, not `sessionId` — mirrors the phone's envelope for this
endpoint exactly.)

`stepsAvgPerDay`/`stepsDaysSpanned`/`sleepQuality` are always present as explicit
keys — `null` when there's no data, never omitted — and are the *primary* source
for those columns (avoids a Markdown parse on the hot path); the same fields also
live in the `.md` frontmatter as a redundant copy, which `reparse.py` falls back
to since it has no envelope to read from. The live endpoint always trusts the
envelope's value over the frontmatter's when both are present. A sender that omits
the keys entirely (rather than sending explicit `null`) is tolerated the same as
any other envelope field — it degrades to "no data available", not an error. The
one stricter check: a `sleepQuality` that *is* present but is neither `null` nor
an integer in `[1,5]` → **400**, nothing written (see the field notes above).

**Part `file`** (`text/markdown`): the raw bytes above.

### Handling — identical steps to `POST /v1/sessions`

1. Bearer auth check.
2. Envelope validation.
3. Hash verification against the `file` part's actual bytes → 422 on mismatch, nothing
   written.
4. Idempotency check on `eventId` (not `sessionId`) — same hash → `200 duplicate`,
   no-op. **Different hash for an existing `eventId` → update, not a duplicate**: overwrite
   the archived `.md` with the new bytes, re-parse, and `UPSERT` the row (new
   `sleep_quality`, new `content_hash`), responding `200` with `status: "updated"`. This is
   no longer rare: the phone re-sends the same `eventId` with a new `contentHash` whenever
   the user rates sleep *after* the measurement already synced (reconnects the band in the
   afternoon and rates then, or taps the faccina a moment after the countdown) —
   `ReadinessRepository.updateSleepQuality()` rewrites the `.md` and re-queues the event
   with `status=PENDING` and the new hash. Same rule `POST /v1/sessions` already applies
   ("changed hash for a known ID = update, not duplicate"); the readiness handler inherits
   it from the shared ingest path — do not add a stricter "eventId already seen → discard"
   shortcut.
5. Write raw bytes to `<RAW_STORAGE_ROOT>/<relPath>` — i.e. `raw/readiness/{id}.md`,
   mirroring the phone's own `gymdata/readiness/{id}.md` layout. On the step-4 update case
   this overwrites the previously archived file, so the raw store stays coherent with the
   row.
6. Parse the YAML frontmatter (reuse the same tolerant parsing approach as the session
   parser — unknown fields ignored, missing numeric fields default to 0, **except**
   `stepsAvgPerDay`/`stepsDaysSpanned`/`sleepQuality`, which stay `null` when missing —
   see field notes above). Then overwrite `stepsAvgPerDay`/`stepsDaysSpanned`/`sleepQuality`
   with the envelope's values (the primary source — see the envelope section above), since
   only the live endpoint has an envelope to read from at all. On success, upsert into
   `readiness_events` (schema below). On failure, log it, keep the raw file, still respond
   `200 stored` — same "raw write and parse are different failure domains" principle as
   sessions.
7. Respond `200 OK`, `{"eventId": "...", "status": "stored"}` (or `"duplicate"` /
   `"updated"`).

### `GET /health` — unchanged, already covers this endpoint too.

## SQLite schema addition

```sql
CREATE TABLE readiness_events (
  id                 TEXT PRIMARY KEY,     -- 8-hex event id (own namespace, not sessions.id)
  measured_at        TEXT NOT NULL,
  readiness          TEXT,                 -- MEASURING|DELOAD_RECOMMENDED|LIGHT_DAY|NORMAL|GOOD|PEAK|NO_BASELINE
  ln_rmssd           REAL,
  resting_hr         INTEGER,
  vo2max             REAL,
  recommendation     TEXT,
  steps_avg_per_day  REAL,                 -- NULL = no data available, never 0
  steps_days_spanned INTEGER,              -- NULL = no data available, never 0
  sleep_quality      SMALLINT,             -- NULL = "non valutato"; 1..5 when rated. CHECK(NULL OR 1..5)
  content_hash       TEXT NOT NULL,
  raw_path           TEXT NOT NULL,
  received_at        TEXT NOT NULL,
  schema_version     INTEGER NOT NULL
);
```

Independent table from `sessions` — a readiness event is not a child of any session (it
fires once per HR connect, whether or not a workout follows). Do not add a foreign key to
`sessions`.

`sleep_quality` was added after the table already existed in deployed DBs — additive,
nullable, no default, no backfill (existing rows stay `NULL` = "non valutato"). SQLite
can't `ALTER TABLE ... ADD CONSTRAINT`, so the `1..5` `CHECK` only guards rows written via
the fresh-DB schema; on a migrated DB every writer (live endpoint, parser) has already
clamped the value to `NULL` or `1..5` before the `INSERT`, so an unconstrained added
column is fine. `PARSER_SCHEMA_VERSION` bumped to 11; `reparse.py --stale` backfills the
column from the frontmatter copy.

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
6. `sleepQuality` in the envelope: an integer `1..5` is stored; explicit `null` stays
   `NULL` (not `0`); an omitted key degrades to `NULL`; anything else present (`0`, `6`,
   `2.5`, `"3"`, `true`) → **400**, nothing written.
7. **Re-rated sleep**: send an `eventId` once (e.g. `sleepQuality: null`), then re-send the
   *same* `eventId` with a rewritten `.md` and a new `contentHash` (`sleepQuality: 4`) →
   `200 {"status": "updated"}`, the row's `sleep_quality`/`content_hash` change, the
   archived `.md` is overwritten, no second row, no `parse_failure`. Re-sending with the
   *same* hash after that → `duplicate`.

## Non-goals (same as the session spec)

No weekly analysis logic here either — `readiness_events` is just another table the
(separate, future) analysis job can query. No new auth model, no public exposure beyond
what's already set up via Tailscale Serve for `/v1/sessions`.
