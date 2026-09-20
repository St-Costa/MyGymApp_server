# API reference

Single-page contract for every endpoint: which id field it uses, which statuses it
returns, and which error means what. The per-record rationale and Markdown/binary shapes
live in the specs (`docs/sync-ingestion/`, `docs/backup/`); this page is the lookup
table so nobody has to reconstruct the contract from five specs plus `app/routes.py`.

FastAPI also serves auto-generated OpenAPI at `/docs`, but the ingest endpoints take
`Form(str)` envelope parts (not typed models), so its multipart schemas are weak —
this page wins on disagreements about the envelope shape.

## Auth

- Every `/v1/*` endpoint requires `Authorization: Bearer <SYNC_SHARED_SECRET>` → else `401`.
- Open (no auth): `GET /health`, `/`, `/exercises`, `/exercises/{id}`,
  `/settings/token`, `POST /settings/token/rotate`, `/static/*`.

## Text-record ingestion (sessions, readiness, scale)

`multipart/form-data` with an `envelope` JSON part and a `file` Markdown part. The three
endpoints share `_handle_ingest` and differ only in the id field:

| Endpoint | Id field | Response id key | Commit bucket |
|---|---|---|---|
| `POST /v1/sessions` | `sessionId` | `sessionId` | `sessions` |
| `POST /v1/readiness` | `eventId` | `eventId` | `readiness` |
| `POST /v1/scale-weighins` | `weighInId` | `weighInId` | `scale` |

Required envelope keys: the id field plus `relPath`, `contentHash` (`"sha256:<hex>"` of
the `file` bytes), `appVersion`, `clientSentAt`. Readiness additionally reads optional
`stepsAvgPerDay` / `stepsDaysSpanned` (explicit `null` = no data, never 0) and
`sleepQuality` (`null` or int 1..5 — any other present value is a hard `400`).

```jsonc
// envelope — POST /v1/sessions
{
  "sessionId":   "3c4d5e6f",
  "relPath":     "2026/08/2026-08-28_rt-71284f58_3c4d5e6f.md",
  "contentHash": "sha256:9f8e7d6c…", // hash of the file part
  "appVersion":  "1.0-phaseNN",
  "clientSentAt":"2026-08-28T20:11:03Z"
}
```

Response statuses: `stored` (new), `updated` (known id, new hash), `duplicate` (known
id, same hash — safe no-op, nothing written). A 2xx means "raw bytes fsynced + hash
verified", not "committed" (git is debounced) — and a parse failure still returns 2xx
(the raw file is safe; the row stays re-derivable via `reparse.py`).

### Bulk sessions

`POST /v1/sessions/bulk` — one multipart request for N sessions, one debounced commit.

`multipart/form-data`:
- part `envelope` (application/json): JSON array of session envelopes
  `[{"sessionId": ..., "relPath": ..., "contentHash": ..., "appVersion": ..., "clientSentAt": ...}, ...]`
- parts `file_0`, `file_1`, … (any content type): raw Markdown bytes for each session by index.

Response `200`:
```jsonc
{
  "results": [
    {"sessionId": "...", "status": "stored"|"duplicate"|"error", "error": ...?},
    ...
  ]
}
```
In the same order as `envelope`. A per-entry hash mismatch is that entry's
`"error"`, not a request failure. `413` if the batch exceeds `BULK_MAX_FILES`
(500) entries or `BULK_MAX_TOTAL_BYTES` (50 MB) total.

```bash
curl -X POST "$HOST/v1/sessions/bulk" -H "Authorization: Bearer $SECRET" \
  -F 'envelope=[...json array...];type=application/json' \
  -F 'file_0=@session1.md' -F 'file_1=@session2.md'
```

## ECG ingestion

Same shape, but the `file` part is gzip-compressed binary (`application/gzip`) and the
hash covers the **compressed** bytes ("hash what's on the wire"):

| Endpoint | Id field | Extra required key | Statuses |
|---|---|---|---|
| `POST /v1/ecg` | `sessionId` | `encoding` | always `stored` (even on overwrite) |

```jsonc
// envelope — POST /v1/ecg
{
  "sessionId":   "3c4d5e6f",
  "relPath":     "ecg/3c4d5e6f.ecg",
  "contentHash": "sha256:…", // hash of the gzip bytes, not the decompressed waveform
  "encoding":    "gzip",
  "appVersion":  "1.0-phaseNN",
  "clientSentAt":"2026-08-28T20:11:03Z"
}
```

The server gunzips, stores the decompressed bytes, and runs the ECG analysis
on a background worker (never failing the upload — failures stay in the
`analyzed_at IS NULL` queue for `analyze_ecg.py`). Once accepted, the server's
copy is the *only* copy — the phone deletes its local file.

### Bulk ECG

`POST /v1/ecg/bulk` — one multipart request for N ECG recordings, one debounced commit.

`multipart/form-data`:
- part `envelope` (application/json): JSON array of ECG envelopes (includes `encoding: "gzip"`)
  `[{"sessionId": ..., "relPath": ..., "contentHash": ..., "encoding": "gzip", "appVersion": ..., "clientSentAt": ...}, ...]`
- parts `file_0`, `file_1`, … (any content type): raw **gzipped** bytes for each ECG by index.

Response `200`:
```jsonc
{
  "results": [
    {"sessionId": "...", "status": "stored"|"duplicate"|"error", "error": ...?},
    ...
  ]
}
```
Same semantics as `/v1/sessions/bulk` but with ECG's extra `encoding` field.
`413` if the batch exceeds `BULK_MAX_FILES` (500) entries or `BULK_MAX_TOTAL_BYTES` (50 MB) total.

```bash
curl -X POST "$HOST/v1/ecg/bulk" -H "Authorization: Bearer $SECRET" \
  -F 'envelope=[...json array...];type=application/json' \
  -F 'file_0=@ecg1.ecg.gz' -F 'file_1=@ecg2.ecg.gz'
```

## Repo files (exercises/routines)

`POST /v1/repo` — envelope keys `relPath`, `op`, `contentHash` (all required, else
`422`); `op: upsert` needs a `file` part, `op: delete` must not have one. `relPath`
must be a relative path under `exercises/` or `routines/` (else `422`).

| `op` | Stored | Duplicate/absent | Deleted |
|---|---|---|---|
| `upsert` | `stored` | `duplicate` (same bytes on disk — no write, no commit) | — |
| `delete` | — | `already_absent` | `deleted` (soft-move to `deleted/<relPath>.<unix_ts>`) |

Note: `contentHash` is required in the envelope even for `op: delete` but is not
verified there (delete carries no bytes to hash) — advisory, per the original brief §2.3.

`POST /v1/repo/bulk` — one multipart request for N entries, one debounced commit.
`envelope` is a JSON array `[{relPath, op, contentHash}, …]`; `file_0…file_N-1` parts
carry the bytes for the `upsert` entries by index. Response `200` with per-entry
`{relPath, status, error?}` in order (`status: stored|duplicate|deleted|already_absent|
error` — a bad entry is that entry's `"error"`, not a request failure). `413` past
`BULK_MAX_FILES` (500) entries or `BULK_MAX_TOTAL_BYTES` (50 MB).

## Restore (pull-only, bearer)

| Endpoint | Request | Response |
|---|---|---|
| `GET /v1/manifest` | — | `{generatedAt, files: {relPath: sha256}}` (cached, O(1) when unchanged) |
| `GET /v1/file?relPath=…` | any live subtree, not just repo files | raw bytes + `X-Content-SHA256`; `404` if not live, `422` if unsafe |
| `POST /v1/repo/files` | `{"relPaths": [...]}` (`413` past 500) | `multipart/mixed`, one part per path in order; `X-Status: present\|absent`, `X-Content-SHA256` when present |
| `GET /v1/repo/tarball` | `?since=<manifest-hash>` optional | gzip tar of every live file + `X-Manifest-SHA256`; empty tar when `since` matches |

## Analysis (read-only, bearer)

`GET /v1/analysis/*` with optional `?today=YYYY-MM-DD` (defaults to today). All numbers
capped at 2 decimals.

| Endpoint | Shape |
|---|---|
| `/cardio-trend` | dict: 4-week trend card + rhythm counters + alerts |
| `/acwr-trend`, `/rpe-acwr-trend` | dict: ratio + semaphore |
| `/acwr-daily-trend`, `/rpe-acwr-daily-trend`, `/session-load-trend` | list of per-week points |
| `/resting-hr-trend`, `/vo2max-trend` | list of weekly buckets |
| `/scale-trend` | dict of metric → list of weekly buckets |
| `/exercises/{id}/pr` (`?is_daily=&is_warmup=`) | `{exerciseId, allTimeTonnagePr, recentHeaviestSetPr}` |

## Dashboard (no auth, same origin)

`/`, `/exercises`, `/exercises/{exercise_id}` (`?today=` on `/`). Server-rendered
HTML over the same analysis layer — no JSON API. `/settings/token` manages the
shared secret: `POST /settings/token/rotate` generates a new token, activates
it immediately and persists it to `DATA_ROOT/.sync_secret` (which wins over
the `SYNC_SHARED_SECRET` env var on restart).

## Error codes

| Code | Meaning |
|---|---|
| `400` | malformed envelope/body JSON; missing required envelope field (ingest); bad `sleepQuality` |
| `401` | missing/invalid bearer (all `/v1/*`) |
| `404` | `GET /v1/file` for a non-live file |
| `413` | bulk caps exceeded (`repo/bulk`, `repo/files`) |
| `422` | `contentHash` mismatch or malformed; unsafe `relPath`; unknown `op`; bad `{"relPaths": …}` body |
| `500` | only when the raw write itself failed (disk/permissions) — parse and git failures never 500 |
