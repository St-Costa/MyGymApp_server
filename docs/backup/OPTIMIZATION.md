# Backup path optimizations

The incremental backup (`POST /v1/repo`, `GET /v1/manifest`, `GET /v1/file`)
was correct but slow for bulk work: a backfill or a restore is N sequential
requests, and — before this — N `git commit`s and N filesystem walks.

This doc has two halves:

- **Points 1, 4, 5, 6 — server-internal.** No wire-contract change; the
  phone keeps working unmodified.
- **Points 2, 3 — new optional batch endpoints.** The single-file endpoints
  stay as the fallback. The phone opts into these on its own schedule; see
  the contracts in [README.md](README.md#batch-endpoints-optional).

## 1 — Debounced git commit

**Before**: every ingest handler did `git add -A && git commit` inline, one
commit per request. 50 files synced = 50 `git` forks + 50 tree writes on the
request path.

**After** (`app/git_backup.py`): handlers call `request_commit(...)` after
the raw bytes are fsynced. It never blocks and never raises — it accumulates
the burst's per-record-type counts and re-arms a quiet-period timer. A
daemon thread (`_committer_loop`) does **one** `git add -A && git commit`
after `COMMIT_DEBOUNCE_SECONDS` (~3s) of write silence, with a message that
sums the burst:

```
sync 2026-09-04T20:11:03Z — sessions:5 readiness:0 scale:0 ecg:0 repo:42
```

`flush_pending_commit()` forces it immediately; the app lifespan calls it
(via `stop_committer()`) on shutdown so a clean restart never inherits
uncommitted writes, and `ensure_repo()` sweeps in anything a crash left
behind (`git add -A` + a `sync … — sweep uncommitted raw writes` commit).

`git commit` runs `--no-verify --no-gpg-sign --quiet` with the author /
committer identity supplied via `GIT_AUTHOR_*` / `GIT_COMMITTER_*` env (plus
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`) so it never
reads user/system git config. `git config` is still set once in
`ensure_repo()` as a fallback for anyone running git by hand in the repo.

Still: no `git gc` / `repack` on any path — git's automatic gc at defaults
is fine for these tiny text files.

### Durability contract change

A 2xx from any ingest endpoint now means **"raw bytes fsynced + hash
verified"**, not "committed". This matches how the phone already behaved —
it marks a file SENT on the 2xx, and never inspected `git log`. A crash
between the fsync and the debounced commit leaves the files uncommitted on
the working tree; the next `git add -A` (next burst, or `ensure_repo` /
`flush` on restart) sweeps them in. Nothing is lost because the raw files
are the source of truth, exactly as before. A `git commit` failure is no
longer a 500 — it's logged loudly and the next commit recovers.

## 2 — Batch write: `POST /v1/repo/bulk`

**Before**: a backfill or a "send everything queued" was N × `POST /v1/repo`
— N requests, N debounced-commit triggers.

**After** (`app/routes.py`, `app/repo.py`): one multipart request carries a
JSON array of `{relPath, op, contentHash}` plus `file_0…file_N-1` parts for
the `upsert` entries. Every entry is applied independently (a bad hash /
path on one is that entry's `"error"`, not a request failure), then **one**
debounced commit covers the whole batch and the parse for each touched
entry is enqueued. Soft caps: `BULK_MAX_FILES` (500) entries,
`BULK_MAX_TOTAL_BYTES` (50 MB) of file bytes → `413`.

Wire contract in [README.md](README.md#post-v1repobulk). The single
`POST /v1/repo` stays for the fine-of-session push.

## 3 — Batch read: `POST /v1/repo/files`, `GET /v1/repo/tarball`

**Before**: a restore or the "Verifica backup" read-back was N × `GET
/v1/file` — 100+ serial round-trips over Tailscale for a full library.

**After** (`app/routes.py`, `app/repo.py`):

- `POST /v1/repo/files` — body `{"relPaths": [...]}`, response
  `multipart/mixed` with one part per path (present → bytes +
  `X-Content-SHA256`; absent → empty body, `X-Status: absent`). For the
  targeted read-back — the client already knows which paths it wants.
- `GET /v1/repo/tarball?since=<manifest-hash>` — a gzip-compressed tar of
  every live file, `X-Manifest-SHA256` response header. Pass the hash back
  as `?since=` and the tar is empty when nothing changed. For a
  from-scratch restore in one transfer.

Both pull-only. Contracts in
[README.md](README.md#post-v1repofiles--get-v1repotarball).

## 4 — Manifest via cache

**Before**: `GET /v1/manifest` walked `data/raw/` and SHA-256'd every file
on every request.

**After** (`app/repo.py`): the `{relPath: sha256}` map is cached, keyed on
`(git rev-parse HEAD, write-generation)`. The write generation is bumped
whenever a `/v1/repo` upsert or delete actually touches the tree. A manifest
request with neither the HEAD nor the write generation changed since the
last one returns the cached map (only `generatedAt` is refreshed). The first
manifest after a burst still walks the tree but only re-hashes files whose
`(mtime_ns, size)` changed since the previous walk — O(changed), not O(all);
every manifest after that (the restore read-back, a re-run of "Verifica
backup") is O(1).

Not an incremental `relPath → sha256` index: that would need coherence
handling across deletes, parse failures, restart rebuilds, and worker
races, for a walk of a few hundred small files that costs tens of
milliseconds. The re-derivable cache is the right trade here.

## 5 — Soft-delete without a `git` fork

`op: delete` moved the file with `git mv` (a `git` fork per delete). It now
uses `os.rename` into `deleted/<relPath>.<unix_ts>`; the debounced
committer's `git add -A` records both the disappearance and the tombstone.

## 6 — SQL-view parse off the request path

**Before**: `POST /v1/repo` parsed the YAML and upserted the `exercises` /
`routines` row synchronously in the handler.

**After** (`app/repo_worker.py`): the handler fsyncs the bytes, enqueues
`(relPath, contentHash, op)`, and returns. A daemon worker drains the queue,
re-reads each file from disk, and folds it into the SQL view (`upsert` →
parse + upsert row or `record_parse_failure`; `delete` → mark the row
`deleted_at`). The raw file and the SQL view are separate failure domains
(the raw file is the source of truth; the view is a re-derivable index).

The queue is in-memory: a restart drops whatever hadn't been parsed, and
`reparse.py --type exercises|routines --stale` (or `--all`) rebuilds the
whole view from the raw files. `stop_worker()` on the lifespan drains the
queue on a clean shutdown.

Sessions / readiness / scale / ECG still parse synchronously in their own
handlers — out of scope here ("don't touch the existing endpoints").

## Testing

`tests/conftest.py::settle_backup()` drains the parse worker then flushes
the debounced committer. Any test that inspects `git log` or the
`exercises` / `routines` tables right after a `POST` calls it first.
