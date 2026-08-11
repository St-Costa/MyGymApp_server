# `POST /v1/ecg` — raw ECG ingestion spec

Status: **phone side implemented and build-verified**, not yet tested against a live
server round-trip. This document is the handoff spec for `MyGymApp_server` — implement
this endpoint there, mirroring the structure of the existing `SPEC.md` (sessions),
`READINESS_SPEC.md`, and `SCALE_SPEC.md` in that repo's `docs/sync-ingestion/`.

Source of truth for the phone-side implementation this spec describes: `docs/SYNC.md` in
the `MyGymApp` repo (not this one), section "Fourth record type: raw ECG"
(`docs/SYNC.md#fourth-record-type-raw-ecg`). Read that section first — this document only
covers what's new/different for the server team; it does not repeat the general
raw-first/idempotent/content-hash design already established for the other three
endpoints.

## Why this endpoint exists

The phone currently runs the full ECG post-session analysis locally (Pan-Tompkins QRS
detection, arrhythmia markers, HRV metrics — 14 derived values, already synced via the
existing `/v1/sessions` frontmatter) and then discards the raw waveform. This endpoint
lets the phone additionally hand over the **raw waveform** so the server can run a
heavier, more accurate analysis — more CPU budget than a phone has, potentially
ML/LLM-assisted interpretation, and comparison against the user's full history rather
than one isolated session. The phone's own local analysis is unchanged and still feeds
the app's at-a-glance UI; this is purely additive.

## Request shape

```
POST /v1/ecg
Content-Type: multipart/form-data
Authorization: Bearer <shared-secret>          (same token as the other 3 endpoints)

  part "envelope" (application/json):
    {
      "sessionId": "3c4d5e6f",
      "relPath": "ecg/3c4d5e6f.ecg",
      "contentHash": "sha256:9f8e7d6c...",       // over the COMPRESSED bytes, see below
      "encoding": "gzip",
      "appVersion": "1.0-phase34",
      "clientSentAt": "2026-08-11T10:05:42Z"
    }
  part "file" (application/gzip):
    <gzip-compressed raw bytes of the .ecg binary format — see "Binary format" below>
```

Response contract — **identical** to the other three endpoints:

```
200 OK  { "sessionId": "3c4d5e6f", "status": "stored" }       // first time
200 OK  { "sessionId": "3c4d5e6f", "status": "duplicate" }    // already had this hash
400/422 { "error": "..." }                                    // envelope malformed / hash mismatch
401     (missing/wrong bearer token)
```

### What's different from `/v1/sessions` / `/v1/readiness` / `/v1/scale-weighins`

1. **The file part is binary and gzip-compressed**, not a markdown/YAML text file. Media
   type is `application/gzip`, not `text/markdown`. The server must gunzip the body before
   parsing the ECG binary format, but must hash-check the **compressed** bytes first (see
   next point) — do not decompress-then-hash.
2. **`contentHash` is computed over the gzip-compressed bytes**, i.e. exactly the bytes
   in the multipart `file` part, before any decompression. This matches the other three
   endpoints' rule ("hash what's on the wire"), just applied to compressed instead of raw
   bytes. Verify the hash against the received compressed bytes; reject with 422 on
   mismatch (per the existing rule in `SYNC.md` §2.2 step 3) *before* attempting to
   gunzip — a corrupted upload should never reach the decompressor.
3. **`relPath` convention**: `ecg/{sessionId}.ecg` (flat, no date-based subdirectory,
   unlike sessions' `YYYY/MM/...`) — matches the phone's own `gymdata/ecg/{sessionId}.ecg`
   layout. Suggest storing server-side at `~/gym-server-data/raw/ecg/{sessionId}.ecg`
   (gunzipped — store the decompressed original, not the compressed transport encoding,
   so the raw store stays directly usable by any future analysis tooling without needing
   to know it was ever gzipped).
4. **No update case (§2.2 step 5 doesn't apply here)**: sessions/readiness/scale can be
   legitimately re-sent with a different hash for a known ID (e.g. rename-sync rewrites a
   session file). An ECG recording for a given `sessionId` is captured exactly once and
   never rewritten on the phone — if a resend for a known `sessionId` arrives with a
   **different** hash, that's unexpected (possibly a bug, possibly a legitimately retried
   "Resync all" after a partial local failure) and should be logged distinctly from the
   idempotent-duplicate case, though returning `200 stored` (overwrite) rather than
   erroring is still the pragmatic choice — don't invent a new rejection path for this.
5. **Timeouts on the phone side are longer** (90s write / 60s read vs. 30s/30s for the
   other three) because even gzip-compressed, a full-session ~130Hz Int16 stream is larger
   than a session/readiness/weigh-in YAML file. No server-side action needed for this, but
   worth knowing when debugging a slow-upload report — a legitimately large file taking
   longer than the other endpoints' typical latency is expected, not necessarily a bug.
6. **Retention asymmetry — read this carefully**: for sessions/readiness/scale, the phone
   keeps a **permanent** local copy regardless of sync status — the server's copy is a
   convenience/analysis mirror. For ECG, the phone **deletes its local copy once upload is
   confirmed** (and unconditionally after 30 days pending, confirmed or not — see
   "Phone-side retention" below). This means **the server's raw store is the only copy of
   the waveform that will ever exist**, once the phone's local file is gone. Standard
   backup discipline for `raw/ecg/` (whatever backup strategy is already used for
   `raw/ecg/`'s sibling `raw/` sessions directory, at minimum) matters more here than for
   the other three record types — losing the server's copy of an ECG recording is
   unrecoverable, not "re-derivable by asking the phone to resend."

## Binary format the server must parse

The phone has no shared code with the server (documented policy, see `SYNC.md`'s "core
design problem" section) — this is a from-scratch second implementation of the reader,
same as the YAML frontmatter parser already written for sessions. Full authoritative
description: `docs/STORAGE.md#raw-ecg-ecgsessionidecg` in the `MyGymApp` repo.

```
Offset 0,  8 bytes : magic "MYGMECG1" (ASCII, no null terminator)
Offset 8,  4 bytes : sample rate, Int32 BIG-ENDIAN (typically 130 — Polar H10 default)
Offset 12, 8 bytes : start timestamp, Int64 BIG-ENDIAN, ns since epoch
                     (currently unused by the phone's own reader — safe to store/ignore,
                     but don't assume it's always 0 or garbage; it is a real value, just
                     not yet consumed by EcgAnalyzer.readFile on the phone)
Offset 20, N×2 bytes: sample stream, Int16 BIG-ENDIAN, microvolts (µV), one per sample,
                      clamped to Short.MIN_VALUE..Short.MAX_VALUE by the phone's writer
```

This is the **decompressed** format — i.e. what you get after gunzipping the multipart
`file` part. Validate `magic == "MYGMECG1"` before trusting the rest of the header;
reject/park anything else the same way the phone's own `EcgAnalyzer.readFile` does
(phone-side: requires file ≥ 32 bytes and ≥ `sampleRate * 5` samples before attempting
analysis — a reasonable minimum sanity check to mirror server-side, though the server
should still **store** anything that passes the hash check even if it's too short to
usefully analyze, same raw-write-vs-parse split as the sessions endpoint).

## Suggested SQLite table

Mirrors the `schema_version` + re-parse discipline already used for `sessions` (see main
`SPEC.md` / `SYNC.md` §2.4):

```sql
CREATE TABLE ecg_recordings (
  session_id       TEXT PRIMARY KEY REFERENCES sessions(id),
  raw_path         TEXT NOT NULL,        -- relative path under raw/ecg/, decompressed
  sample_rate      INTEGER NOT NULL,
  start_timestamp_ns INTEGER,            -- from the header; currently informational only
  duration_seconds REAL,                 -- derivable from sample count / sample_rate
  content_hash     TEXT NOT NULL,        -- of the compressed bytes as received (audit trail)
  received_at      TEXT NOT NULL,
  analyzed_at      TEXT,                 -- NULL until server-side analysis has run
  schema_version   INTEGER NOT NULL
);
```

`analyzed_at IS NULL` is the natural work queue for whatever analysis job gets built next
(out of scope for this transport spec — see `SYNC.md`'s "New analysis this unlocks
server-side" note). Results of that analysis belong in their own table(s), designed
alongside the analysis job itself, not here.

## Receiver logic (step by step)

Mirrors `POST /v1/sessions` (`SYNC.md` §2.2) with the deltas called out above:

1. Bearer auth check (same shared secret as the other three endpoints). 401 on
   missing/wrong token.
2. Parse `envelope` JSON, validate required fields present (`sessionId`, `relPath`,
   `contentHash`, `encoding`, `appVersion`, `clientSentAt`).
3. Read the `file` part's raw (still-compressed) bytes. Compute sha256, compare to
   `envelope.contentHash`. Reject with 422 on mismatch — **before** attempting to gunzip.
4. Idempotency check: does a row already exist for `sessionId` with this exact
   `contentHash`? If yes → `200 duplicate`, no write. (Handles retried requests and
   re-sent "Resync all" runs for files still on the phone.)
5. If `sessionId` exists with a **different** hash: unexpected per point 4 above, but
   don't hard-reject — log it distinctly, then treat as an overwrite (same as sessions'
   "different hash for known id = update" rule), not a new-row insert.
6. Gunzip the verified-good compressed bytes.
7. Validate the decompressed bytes start with magic `MYGMECG1` (see "Binary format"
   above). If invalid, still write the raw bytes to disk if you want to preserve
   for-forensics evidence of a corrupted client-side gzip, but log loudly — this
   shouldn't happen if step 3's hash check passed, since the hash covers exactly what was
   gunzipped.
8. Write the **decompressed** bytes to disk: `~/gym-server-data/raw/ecg/{sessionId}.ecg`
   (or per `relPath` from the envelope — same value, provided for consistency with the
   other three endpoints' convention of trusting the phone's relative path rather than
   reconstructing it server-side).
9. Parse the header fields (`sample_rate`, `start_timestamp_ns`, `duration_seconds` from
   sample count), upsert into `ecg_recordings` keyed by `session_id`, `analyzed_at = NULL`.
   Same failure-domain split as sessions (`SYNC.md` §2.2, steps 6/7 discussion): if header
   parsing throws, the raw file write (step 8) has already succeeded and is durable —
   catch the parse exception, log it, still return `200 stored`. A schema/format you
   haven't taught the parser about yet must never cause data loss or a phone-visible
   failure.
10. Respond `200 stored`.

## Phone-side retention (context for the server team, not something the server enforces)

So the server team understands the delivery guarantees they're building against:

- The phone queues the raw `.ecg` file for upload right after the session is saved
  (`ActiveRoutineViewModel.registerRoutine()`), gzip-compresses it, and retries on a
  4-hourly durability net plus an immediate expedited attempt — same retry discipline as
  the other three pipelines (`SYNC.md` §3.2).
- The phone deletes its local `.ecg` file **only after this endpoint confirms `stored` or
  `duplicate`** (i.e. any 2xx response) — so as long as the server eventually comes back
  up and this endpoint works, no data is lost even after a multi-day outage.
- **Exception**: if a recording sits `PENDING`/`FAILED` locally for **30 days** without a
  confirmed upload, the phone gives up and deletes the file anyway (`EcgSyncLedgerRepository
  .expireStale()`), to bound local storage growth. If the server is down for longer than
  that, that session's raw waveform is gone for good — only the 14 phone-computed summary
  metrics survive (already synced separately via the existing `/v1/sessions` endpoint,
  independent of this one). This is a known, deliberate limitation, not a bug to work
  around server-side — there is nothing the server can do to recover data the phone has
  already deleted.
- Because of the above, once this endpoint accepts a recording, **it is the only copy**.
  Treat `raw/ecg/` with at least the same backup discipline as `raw/`'s session files, and
  arguably more urgency, since sessions/readiness/scale can always be manually re-derived
  by asking the phone (their local files are permanent) — ECG cannot be, once the phone's
  copy is gone.

## Implementation checklist for `MyGymApp_server`

- [ ] `POST /v1/ecg` receiver (FastAPI, same stack as the other three endpoints)
- [ ] Bearer auth reuse (same shared secret / constant-time compare as existing endpoints)
- [ ] Compressed-bytes hash verification before gunzip
- [ ] Gunzip + `MYGMECG1` magic validation
- [ ] Write decompressed raw bytes to `raw/ecg/{sessionId}.ecg`
- [ ] `ecg_recordings` table + upsert (header fields only; analysis results are a separate,
      later effort)
- [ ] Idempotency (`sessionId` + `contentHash`) and update-on-different-hash handling
- [ ] `schema_version` stamping + inclusion in the existing `reparse.py` pattern, if/when
      header-parsing logic changes
- [ ] Backup/retention plan for `raw/ecg/` specifically called out (see "Retention
      asymmetry" above) — this is the one point in the whole sync design where the server
      becomes a sole point of failure for data the phone won't have anymore
