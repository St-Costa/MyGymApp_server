# Git-versioned backup store + repo-file ingestion

## What it does

Two things, added together (design brief: `docs/backup-server-brief.md`):

1. **`data/raw/` is a git repository.** Successful ingests are committed in
   debounced bursts (one background commit ~3s after a burst settles — see
   [OPTIMIZATION.md](OPTIMIZATION.md) point 1), so any prior state, and any
   accidentally deleted file, is recoverable from history. A 2xx means "raw
   bytes fsynced + hash verified", not "committed".
2. **A fifth record type: "repo files"** (`exercises/*.md`, `routines/*.md`)
   via `POST /v1/repo`, plus **restore endpoints** (`GET /v1/manifest`,
   `GET /v1/file`) the phone uses to pull missing/changed files back down
   ("Ripristina dal server").

The existing four endpoints are unchanged except for the git-commit hook.
The raw-first / tolerant-parse philosophy holds everywhere: a parse failure
still returns `200`, still writes the raw file, still commits.

## `POST /v1/repo`

`multipart/form-data`, `Authorization: Bearer <shared-secret>`, one
`envelope` JSON part and (for `op: upsert`) one `file` part.

```jsonc
// envelope
{
  "relPath":     "exercises/bench-press-ex-a1b2c3d4.md",
  "op":          "upsert",           // "upsert" | "delete"
  "contentHash": "sha256:9f8e7d6c…", // upsert: hash of the file bytes
  "appVersion":  "1.0-phaseNN",
  "clientSentAt":"2026-09-04T20:11:03Z"
}
```

`relPath` must be a plain relative path under `exercises/` or `routines/`.
Anything with `..`, a leading `/`, an absolute path, or a target outside
those two subtrees is rejected `422` — nothing written.

### `op: "upsert"`

1. Bearer check → `401`.
2. Envelope has `relPath`, `op`, `contentHash` → else `422`.
3. `relPath` safety check → `422`.
4. SHA-256 of the `file` bytes matches `contentHash` → else `422`.
5. If `data/raw/<relPath>` already exists with that exact hash →
   `200 {"status":"duplicate"}`, **no write, no commit**.
6. Atomic write (temp file + `fsync` + `os.replace`).
7. `200 {"relPath": "...", "status": "stored"}` — returned **now**, once
   the bytes are fsynced. The parse into the `exercises` / `routines` SQL
   view runs off the request path (a worker thread), and the git commit is
   debounced — see [OPTIMIZATION.md](OPTIMIZATION.md) points 1 & 6. A parse
   error is logged and recorded in `parse_failures`, never surfaced.

### `op: "delete"`

No `file` part. Sent when an exercise/routine is deleted, or a rename made
the old `{slug}-{id}.md` path obsolete.

- File absent → `200 {"status":"already_absent"}`, no commit.
- **Soft delete** — never `rm`. `os.rename` into
  `data/raw/deleted/<relPath>.<unix_ts>` (the debounced commit records the
  move; see [OPTIMIZATION.md](OPTIMIZATION.md) point 5). The SQL row is
  marked `deleted_at` by the parse worker, never dropped. Commit message
  notes it: `repo:1 (delete … → deleted/)`.
- `200 {"relPath": "...", "status": "deleted"}`.

`deleted/` grows slowly and is never served by `/v1/manifest` or
`/v1/file`. Trim it manually (`git rm` + commit) if it ever needs it.

### Debug test file

The phone's Options → "Test backup verso il server" button sends an `upsert`
then a `delete` for `routines/_debug-backup-{epochMillis}.md`. It's a normal
request in every way except one: a basename starting with `_` is **written
and committed but never produces a `routines` / `exercises` row** (logged at
debug level and skipped). Expect one tombstone per button press; harmless.

## Restore endpoints

### `GET /v1/manifest`

```jsonc
{
  "generatedAt": "2026-09-04T20:15:00Z",
  "files": {
    "2026/08/2026-08-28_rt-71284f58_b38ae530.md": "sha256:…",
    "exercises/bench-press-ex-a1b2c3d4.md":        "sha256:…",
    "routines/pull-rt-71284f58.md":                "sha256:…"
    // …every live file except deleted/ and .git/
  }
}
```

Cached and served O(1) when nothing changed since the last call (keyed on
`git rev-parse HEAD` + a write-generation counter) — see
[OPTIMIZATION.md](OPTIMIZATION.md) point 4. The first manifest after a burst
still pays one full walk + hash of every file, which is fast enough for a
few hundred small files.

### `GET /v1/file?relPath=<relPath>`

Raw bytes of one live file, `Content-Type: text/markdown` for `.md` (else
`application/octet-stream`), plus an `X-Content-SHA256: sha256:…` header.
`404` if it isn't a live file, `422` if `relPath` fails the safety check.
Unlike `/v1/repo` this accepts any live subtree (sessions/readiness/scale/
ecg included) — a restore pulls everything, not just repo files. Pull-only:
the phone never asks the server to delete anything during a restore.

## Batch endpoints (optional)

These exist so a backfill / restore / verify is **one round-trip**, not N.
The single-file endpoints above stay as the fallback — a client can adopt
these at its own pace. See [OPTIMIZATION.md](OPTIMIZATION.md) points 2 & 3.
All three take the same bearer token.

### `POST /v1/repo/bulk`

N upserts/deletes in one request, **one** git commit for the whole batch.

```
POST /v1/repo/bulk
Content-Type: multipart/form-data
Authorization: Bearer <shared-secret>

  part "envelope" (application/json):
    [
      {"relPath": "exercises/bench-ex-a1b2c3d4.md", "op": "upsert", "contentHash": "sha256:…"},
      {"relPath": "routines/old-rt-99999999.md",    "op": "delete", "contentHash": "sha256:…"},
      …
    ]
  parts "file_0", "file_1", …  (any content type):
    raw bytes for the entry at that index — present iff that entry's op is "upsert"
```

Response `200`:

```jsonc
{
  "results": [
    {"relPath": "exercises/bench-ex-a1b2c3d4.md", "status": "stored"},
    {"relPath": "routines/old-rt-99999999.md",    "status": "already_absent"},
    {"relPath": "exercises/bad.md", "status": "error", "error": "contentHash mismatch: …"}
  ]
}
```

- One `results` entry per `envelope` entry, **same order**. `status` is one
  of `stored` / `duplicate` / `deleted` / `already_absent` / `error`; an
  `error` entry also has an `error` string.
- A per-entry hash mismatch, bad path, or missing `file_i` is that entry's
  `error` — the request still returns `200` and the other entries still
  apply. Only auth (`401`), malformed envelope (`400`/`422`), or exceeding
  **500 entries / 50 MB** of file bytes (`413`) fail the whole request.
- Entries apply top to bottom, so an `upsert` then a `delete` of the same
  `relPath` in one batch ends deleted.
- Same debounced commit + off-request-path parse as `POST /v1/repo`: a 2xx
  means every non-`error` entry's bytes are fsynced.

### `POST /v1/repo/files` & `GET /v1/repo/tarball`

**`POST /v1/repo/files`** — fetch many live files in one round-trip (the
"Verifica backup" read-back, a targeted restore):

```
POST /v1/repo/files            body: {"relPaths": ["exercises/…", "readiness/…", …]}
→ 200  Content-Type: multipart/mixed; boundary="…"
```

One part per requested `relPath`, in order. Each part:
`Content-Disposition: attachment; name="<relPath>"`, `X-Status: present|absent`,
and — for `present` — `X-Content-SHA256: sha256:…` + a `Content-Type` +
the raw bytes as the part body. An `absent` part (missing file, or one that
fails the safety check) has an empty body. `413` over 500 paths.

**`GET /v1/repo/tarball?since=<manifest-hash>`** — a from-scratch restore in
one transfer:

```
GET /v1/repo/tarball
→ 200  Content-Type: application/gzip
       X-Manifest-SHA256: sha256:…        ← current manifest hash
       <gzip-compressed tar of every live file>
```

The tar's member names are relPaths. Pass the `X-Manifest-SHA256` value back
as `?since=` next time; when it still matches, the tar is empty (nothing
changed). Both endpoints are pull-only.

## The git repo

- Initialized on startup if `data/raw/.git` is missing (`git init` +
  `user.name`/`user.email`/`commit.gpgsign=false`, then a first
  "import existing store" commit if there are already files). Idempotent;
  also sweeps in any writes a previous run left uncommitted.
- **Commits are debounced** (see [OPTIMIZATION.md](OPTIMIZATION.md) point 1):
  a background thread does one `git add -A && git commit` per burst, ~3s
  after the last write, not one per request. Commit message sums the burst:
  `sync <ISO8601 UTC> — sessions:N readiness:N scale:N ecg:N repo:N`
  (counts of files touched across the whole burst). A backfill of dozens of
  files is one commit.
- **A 2xx means "raw bytes fsynced + hash verified", not "committed."** This
  is the phone's existing notion of SENT. A crash between the fsync and the
  debounced commit leaves files uncommitted on the working tree; the next
  `git add -A` (next burst, or the startup sweep) picks them up. Nothing is
  lost — the raw files are the source of truth. A `git commit` failure is
  logged loudly, not surfaced as a 500.
- A duplicate delivery or an already-absent delete writes nothing → no
  commit.
- The SQLite DB stays at `data/db.sqlite3`, **outside** `data/raw/` — never
  committed.
- No `git gc --aggressive`, no repack cron. Tiny text files; git's default
  automatic gc is fine.

### Optional off-box mirror

Set `BACKUP_GIT_REMOTE` (a bare repo on another disk, or a private
GitHub/Gitea URL with a deploy key). After each local commit the server
does one best-effort `git push --quiet <remote> HEAD:main`. A push failure
is logged, **not** fatal — the local commit is the real backup.

## Recovering a file

```bash
# what happened to it
git -C data/raw log --oneline -- <path>
# its content at a given commit
git -C data/raw show <commit>:<path>
# or just look in the working tree
ls data/raw/deleted/
```

## The SQL view (`exercises` / `routines` / `routine_exercises`)

Parallel to `sessions` — a tolerant parse over the YAML shapes in the app
repo's `docs/STORAGE.md` §Exercise / §Routine (unknown keys ignored, missing
numeric fields → `None`, only `id` required). A parse failure logs, records
a `parse_failures` row, and still returns `200` + still commits.

`routines` upserts delete-then-reinsert their `routine_exercises` child
rows, same as sessions' child tables. An upsert of a previously tombstoned
id clears its `deleted_at` (a rename that reuses the id, an undo on the
phone). `_`-prefixed basenames never produce a row.

`reparse.py` re-derives this view from the raw files like every other record
type:

```bash
python reparse.py --type exercises --all
python reparse.py --type routines --stale
python reparse.py ex-a1b2c3d4 rt-71284f58   # specific ids
```

## Related

- `docs/backup-server-brief.md` — the design brief this implements.
- `docs/sync-ingestion/SPEC.md` — the raw-first principle everything here
  inherits.
- App repo `docs/BACKUP.md` — the phone-side half of this design.
