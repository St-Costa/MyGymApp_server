# MyGymApp sync server — implementation spec

## Context

This is a **new, standalone server project** — not part of the MyGymApp Android app repo.
It runs on a self-hosted machine reachable only via Tailscale. Its job: receive workout
session data pushed by the MyGymApp Android app after every gym session, store it
durably, and make it queryable for a weekly analysis job (the analysis job itself is a
future, separate piece of work — not part of this spec).

You (the implementing agent) do not have access to the Android app's codebase. Everything
you need to know about the data format is in this document. Ask the user if anything here
is ambiguous rather than guessing.

**One phone, one server, both owned by the same person.** This is not a multi-tenant
service — no user accounts, no signup, a single shared secret is sufficient auth.

---

## Why this design: raw-first, parse-second

The Android app's on-disk session format changes fairly often (it's an actively developed
personal project). If this server only stored a strict, parsed representation, every
format change on the phone side would risk silently breaking ingestion or losing data.

To avoid that, this server **always stores the exact raw bytes it receives first**, before
attempting to interpret them. Parsing into the queryable SQLite layer is a second,
independent step that is allowed to fail or lag without losing anything — the raw file is
always there to re-parse later once the parser is updated. Keep this principle in mind for
every design decision below: **never let a parsing failure prevent or corrupt the raw
write.**

---

## The data format you will receive

Each "session" is one workout. The phone sends it as the **raw bytes of a Markdown file
with a YAML frontmatter block**, unmodified — this server does not need to understand the
Android app's internal code, only this on-the-wire text format.

### Example session file content

```markdown
---
id: 3c4d5e6f
date: 2025-04-15
routineId: rt-b2c3d4e5
routineName: Push Day
startedAt: 2025-04-15T09:15:00
completedAt: 2025-04-15T10:05:42
totalTonnage: 12450.0
tonnageByBodypart:
  chest: 8200.0
  shoulders: 4250.0
sessionCalories: 412.3
sessionTrimp: 87.5
vo2max: 48.2
ecgBeats: 5240
ecgDurationSec: 2950
ecgSessionRmssd: 38.2
ecgPacCount: 2
ecgPauseCount: 0
ecgIrregularBeats: 4
afibSuspicionEpisodes: 0
sdnn: 55.0
pnn50: 12.4
poincareSd1: 27.1
poincareSd2: 85.6
poincareRatio: 0.317
cardiacDriftBpmMin: 0.42
restingHr: 58
hrr60s: 28
exercises:
  - exerciseId: ex-a1b2c3d4
    exerciseName: Bench Press
    type: FORZA
    bodypart: chest
    excludeFromTonnage: true
    isDaily: true
    sets:
      - reps: 8
        weight: 80.0
      - reps: 6
        weight: 85.0
---

Optional free-text session notes go here, can be empty, can contain arbitrary text
including markdown syntax, blank lines, anything.
```

### Field notes (important — read before writing the parser)

- **YAML frontmatter is delimited by `---` lines** at the very start and after the block;
  everything after the second `---` is a free-text body (may be empty).
- **All numeric fields with value 0 may be omitted entirely** from the YAML (this is a
  space-saving convention on the sender side). Treat any missing numeric field as `0` (or
  `0.0`), never as an error or null-that-breaks-parsing. Fields known to be sometimes
  omitted this way: `ecgBeats`, `ecgDurationSec`, `ecgSessionRmssd`, `ecgPacCount`,
  `ecgPauseCount`, `ecgIrregularBeats`, `cardiacDriftBpmMin`, `restingHr`, `hrr60s`,
  `sdnn`, `pnn50`, `poincareSd1`, `poincareSd2`, `poincareRatio`,
  `afibSuspicionEpisodes`. `tonnageByBodypart` entries with value 0 are also dropped
  entirely (not present as a key at all) rather than written as `0.0`.
- **`completedAt` may be an empty string** (`completedAt: ""`) — this means the session was
  abandoned/not finished. Still store it; don't treat empty as an error.
- **`excludeFromTonnage` and `isDaily` are omitted when `false`** (only appear when `true`).
  Treat a missing key as `false`.
- **Doubles/floats are formatted to 2 decimal places** on the sending side, but don't
  assume that precision — parse as full-precision floats/doubles regardless.
- **New fields may appear in future versions that this spec doesn't list.** Your parser
  should read known fields into typed SQLite columns and silently ignore unrecognized
  YAML keys rather than erroring on them. (The raw file always retains everything — see
  the raw-first principle above.)
- IDs: exercise IDs look like `ex-{8 hex chars}`, routine IDs `rt-{8 hex chars}`, session
  IDs are a bare `{8 hex chars}` string (no prefix).
- `type` on an exercise is one of two string values: `FORZA` or `STRETCH`.

---

## API

### Authentication

Every request must include:
```
Authorization: Bearer <shared-secret>
```
The shared secret is a long random string (e.g. generated with `openssl rand -hex 32`),
configured via an environment variable on the server (e.g. `SYNC_SHARED_SECRET`) — never
hardcoded. Reject any request with a missing or incorrect token with `401 Unauthorized`,
using a constant-time string comparison (not `==`) to avoid timing attacks.

### `POST /v1/sessions`

Receives one workout session. Request is `multipart/form-data` with two parts:

**Part `envelope`** (`application/json`):
```json
{
  "sessionId": "3c4d5e6f",
  "relPath": "2026/08/2026-08-05_rt-b2c3d4e5_3c4d5e6f.md",
  "contentHash": "sha256:9f8e7d6c5b4a3928...",
  "appVersion": "1.0-phase29",
  "clientSentAt": "2026-08-05T10:05:42Z"
}
```

| Field | Meaning |
|---|---|
| `sessionId` | The 8-hex session ID, primary key for this record |
| `relPath` | Relative path the sender uses internally (`YYYY/MM/filename.md`) — use this as the relative path when writing the raw file to disk, so the server's raw store mirrors the sender's own layout |
| `contentHash` | `sha256:` prefix + hex digest of the **exact bytes** of the `file` part, computed by the sender before sending |
| `appVersion` | Free-form string identifying the sender's build, for diagnostics only |
| `clientSentAt` | ISO-8601 UTC timestamp of when the sender initiated the request |

**Part `file`** (`text/markdown`): the raw bytes of the session file, exactly as shown in
the example above.

### Request handling — do these steps in order, exactly

1. **Auth check.** Missing/wrong bearer token → `401`. Do this before touching the body.
2. **Envelope validation.** Parse the `envelope` JSON part. If it's missing required
   fields or malformed JSON → `400 Bad Request` with a JSON body `{"error": "..."}`
   describing what's wrong.
3. **Hash verification.** Compute SHA-256 of the raw bytes of the `file` part. Compare to
   `envelope.contentHash` (strip the `sha256:` prefix first). If they don't match →
   `422 Unprocessable Entity`, **do not** write anything to disk or the database. This
   catches corrupted/truncated uploads.
4. **Idempotency check.** Look up `sessionId` in the database.
   - Not found → this is a new session. Continue to step 5 (insert).
   - Found, and stored `content_hash` **matches** the incoming hash → this is a retried
     duplicate delivery (the sender didn't see the previous success response). Respond
     `200 OK` with `{"sessionId": "...", "status": "duplicate"}` and **do not** re-write
     anything — this must be a safe no-op.
   - Found, and stored `content_hash` **differs** from the incoming hash → the session was
     edited after a previous sync (this is a legitimate case — the sender may rewrite a
     session file, e.g. after a rename elsewhere in its own data). Treat as an update:
     continue to step 5, but overwrite rather than insert.
5. **Write the raw file to disk**, path `<RAW_STORAGE_ROOT>/<relPath>` (from the envelope),
   creating parent directories as needed. This is the durable source of truth — this step
   must succeed and be confirmed (flushed) before continuing. If this write fails for any
   reason (disk full, permissions), respond `500` and do not proceed to step 6 — do not
   record a partial/inconsistent state in the database.
6. **Parse the YAML frontmatter** out of the file just written, using the field rules in
   the "Field notes" section above. Wrap this whole step in its own try/except:
   - **On success**: upsert a row into the `sessions` table (see schema below) plus rows
     into `exercise_sets` and `tonnage_by_bodypart` (delete-then-reinsert those child rows
     on an update, so a re-sent session doesn't leave stale extra rows from a previous,
     longer version of itself). Set `schema_version` to the current parser version
     constant (see below).
   - **On failure** (unexpected shape, parse exception): log the exception with the
     `sessionId` and file path for later debugging, but **do not fail the request**. The
     raw file (step 5) is already safely stored — that is what matters for not losing data.
     Still respond `200 OK` — the sender's job (reliable delivery) succeeded regardless of
     whether this server's parser currently understands the content. You may still record
     a bare-minimum row in `sessions` (just `id`, `content_hash`, `raw_path`,
     `received_at`, `schema_version = 0` meaning "unparsed") so the reparse tool (see
     below) can find and retry it later — or track "unparsed" sessions any other way you
     find natural (e.g. a separate `parse_failures` table); document whichever approach
     you pick.
7. Respond `200 OK`, body `{"sessionId": "...", "status": "stored"}` (or `"updated"` if it
   was an overwrite in step 4 — your choice whether to distinguish `"updated"` from
   `"stored"` in the response, it doesn't need to be a separate case if that's simpler).

### `GET /health`

Trivial endpoint, no auth required, returns `200 OK` with any small body. Used to verify
the server + Tailscale Serve path is reachable at all, independent of the sync logic.

---

## Storage layout on disk

Pick a root directory for all server data, e.g. `~/mygymapp-server-data/`, containing:

```
mygymapp-server-data/
├── raw/                  ← raw session files, exact mirror of relPath from each envelope
│   └── 2026/08/2026-08-05_rt-b2c3d4e5_3c4d5e6f.md
├── db.sqlite3            ← the parsed/queryable layer
└── logs/                 ← application logs (parse failures, request errors, etc.)
```

Make this root configurable via an environment variable (e.g. `DATA_ROOT`), not
hardcoded, so it can point at wherever the user wants persistent storage on their box.

---

## SQLite schema

```sql
CREATE TABLE sessions (
  id                       TEXT PRIMARY KEY,   -- 8-hex session id
  date                     TEXT,
  routine_id               TEXT,
  routine_name             TEXT,
  started_at               TEXT,
  completed_at             TEXT,               -- may be '' for abandoned sessions
  total_tonnage            REAL,
  session_calories         REAL,
  session_trimp            REAL,
  vo2max                   REAL,
  ecg_beats                INTEGER,
  ecg_duration_sec         INTEGER,
  ecg_session_rmssd        REAL,
  ecg_pac_count            INTEGER,
  ecg_pause_count          INTEGER,
  ecg_irregular_beats      INTEGER,
  afib_suspicion_episodes  INTEGER,
  sdnn                     REAL,
  pnn50                    REAL,
  poincare_sd1             REAL,
  poincare_sd2             REAL,
  poincare_ratio           REAL,
  cardiac_drift_bpm_min    REAL,
  resting_hr               INTEGER,
  hrr60s                   INTEGER,
  notes                    TEXT,               -- the free-text body after the frontmatter
  content_hash             TEXT NOT NULL,
  raw_path                 TEXT NOT NULL,       -- relPath, for re-parse / audit
  received_at              TEXT NOT NULL,       -- server-side timestamp, ISO-8601 UTC
  schema_version            INTEGER NOT NULL     -- parser version that last wrote this row
);

CREATE TABLE exercise_sets (
  session_id            TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  exercise_id           TEXT NOT NULL,
  exercise_name         TEXT,
  exercise_type         TEXT,                  -- 'FORZA' | 'STRETCH'
  bodypart              TEXT,
  set_index             INTEGER NOT NULL,       -- 0-based position within this exercise
  reps                  INTEGER,
  weight                REAL,
  exclude_from_tonnage  INTEGER NOT NULL DEFAULT 0,  -- 0/1 boolean
  is_daily              INTEGER NOT NULL DEFAULT 0   -- 0/1 boolean
);
CREATE INDEX idx_exercise_sets_session ON exercise_sets(session_id);

CREATE TABLE tonnage_by_bodypart (
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  bodypart    TEXT NOT NULL,
  tonnage     REAL NOT NULL
);
CREATE INDEX idx_tonnage_by_bodypart_session ON tonnage_by_bodypart(session_id);
```

On an update to an existing session (step 4 → step 6 overwrite case), delete existing
`exercise_sets` and `tonnage_by_bodypart` rows for that `session_id` before re-inserting,
so the child tables never accumulate stale rows from a previous version of the same
session.

### `schema_version` and the reparse tool

Define a single integer constant in the codebase, e.g. `PARSER_SCHEMA_VERSION = 1`,
stamped into every row the parser writes. Bump it by hand whenever you change what fields
the parser extracts.

Write a small standalone script, `reparse.py` (or equivalent), that:
1. Accepts either `--all` or a list of specific `session_id`s / raw paths.
2. For each, re-reads the **raw file already on disk** (never touches the network — this
   is purely local re-processing) and re-runs the *same* parsing function the live
   `POST /v1/sessions` handler uses in step 6 (factor that logic into one shared function
   called from both places — do not duplicate the parsing logic between the live endpoint
   and this script).
3. Upserts the result exactly as step 6 does.

This is the mechanism for catching the database up after you improve the parser to handle
a new field — you never need the phone involved, because the raw files already have
everything they ever sent.

A reasonable extra: a query/CLI flag like `--stale` that finds
`SELECT id, raw_path FROM sessions WHERE schema_version < <current constant>` and reparses
just those, for convenience.

---

## Non-goals / explicitly out of scope for this implementation

- **No weekly analysis logic** — this spec covers ingestion and storage only. The analysis
  job that reads from this SQLite database is a separate, future piece of work.
- **No multi-user / accounts** — single shared secret is the entire auth model.
- **No public internet exposure** — this service is only ever reached via Tailscale
  (see below). Do not add CORS handling, rate limiting for abuse, or anything aimed at
  defending against the open internet; that would be solving a problem that doesn't exist
  here and adding complexity for no benefit. Bind the HTTP server to `localhost` only —
  never `0.0.0.0` — since Tailscale Serve reverse-proxies to localhost and there's no
  reason for the port to be reachable any other way.
- **No other record types yet** — this may later grow a second endpoint (e.g. for body
  scale readings from an unrelated integration), but do not build speculative
  generalization for that now. If you want to leave a small amount of headroom, prefer
  clear/boring code over a premature abstraction (e.g. it's fine if `POST /v1/sessions` is
  simply its own concrete handler rather than a generic "record ingestion" framework).

---

## Suggested stack (not mandatory, but this is the assumed default — deviate only with a good reason and say so)

- **Python + FastAPI** for the HTTP layer (good multipart support, async, easy to run
  under systemd or Docker on a self-hosted box).
- **`sqlite3`** (stdlib) or a very light wrapper — no need for a full ORM for a
  single-table-family personal project.
- **PyYAML** or `ruamel.yaml` for parsing the frontmatter block.
- Config via environment variables: `SYNC_SHARED_SECRET`, `DATA_ROOT`, `PORT` (default e.g.
  `8420`, anything unlikely to collide — confirm with the user if unsure).
- Run under `systemd` (a `.service` unit) or Docker, whichever the user's box already uses
  for other self-hosted services — ask if unclear, don't assume.

---

## Tailscale Serve setup (do this, do NOT use Tailscale Funnel)

This server must be reachable **only from devices on the user's own Tailscale tailnet**
(the phone running MyGymApp is one of those devices, with the Tailscale Android app
installed and authenticated to the same tailnet). This is a private, tailnet-internal
connection — there is no reason for this service to be reachable from the public internet.

**Use `tailscale serve`, not `tailscale funnel`.** Funnel exposes a service to the public
internet through Tailscale's relay; Serve exposes it only to other tailnet members over
HTTPS with a valid cert via MagicDNS. Funnel would be actively wrong for this use case —
do not set it up, do not suggest it as an alternative, unless the user explicitly says the
sending device will sometimes be off-tailnet (it won't, per the current design).

Setup steps to perform/document for the user:
1. Ensure `tailscale up` has been run on this machine and it's joined to the tailnet.
2. Start the FastAPI app bound to `127.0.0.1:<PORT>` only.
3. Run `tailscale serve --bg https / http://localhost:<PORT>` — the `--bg` flag persists
   the mapping across reboots. Verify with `tailscale serve status`.
4. The resulting hostname (visible via `tailscale status` or `tailscale serve status`,
   form `https://<machine-name>.<tailnet-name>.ts.net`) is what the phone will be
   configured to POST to — surface this clearly at the end of setup (e.g. print it, or put
   it in a README) since the user needs to copy it into the Android app's settings.
5. Confirm MagicDNS is enabled on the tailnet (this is usually on by default) — required
   for the `.ts.net` hostname to resolve.

---

## Testing checklist (do this before considering the implementation done)

1. `GET /health` responds `200` locally (`curl http://localhost:<PORT>/health`) and via the
   Tailscale Serve hostname from another tailnet device if possible.
2. `POST /v1/sessions` with a valid envelope + a real (or hand-written, matching the
   example above) session `.md` file succeeds, writes the raw file to the right path under
   `DATA_ROOT/raw/`, and a matching row appears in `sessions` (verify with
   `sqlite3 db.sqlite3 "SELECT * FROM sessions;"`).
3. Re-sending the **exact same** request (same hash) returns `{"status": "duplicate"}` and
   does not create a second row or duplicate the raw file content.
4. Sending a request with a `contentHash` that doesn't match the actual file bytes returns
   `422` and nothing is written anywhere.
5. Sending a request with a missing/wrong bearer token returns `401`.
6. Sending a session `.md` file with several of the omittable numeric fields (see Field
   notes) actually missing from the YAML — confirm those columns end up `0`/`0.0` in
   SQLite, not `NULL` or an error.
7. Sending an update for a `sessionId` that already exists, with different file content
   (different hash) — confirm the `sessions` row is overwritten (not duplicated) and old
   `exercise_sets`/`tonnage_by_bodypart` rows for that session are gone, replaced by the
   new ones.
8. Run `reparse.py --all` against a database with a couple of stored sessions and confirm
   it reproduces the same rows without needing any network access.

---

## Deliverables

- Working FastAPI app implementing the endpoints above.
- SQLite schema creation (a small init script or auto-create-on-startup — your choice).
- `reparse.py` maintenance script sharing parser code with the live endpoint.
- A `README.md` covering: how to configure env vars, how to run it (systemd unit or
  Dockerfile — pick one, document it), and the Tailscale Serve setup steps, ending with
  "here's the hostname to put into the MyGymApp Android app's settings."
- Brief inline comments explaining the raw-first / idempotency / schema-version reasoning
  at the relevant code points, so a future maintainer (possibly a future Claude session
  with no memory of this spec) understands *why*, not just *what*.
