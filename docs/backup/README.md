# Git-versioned backup store + repo-file ingestion

## What it does

Two things, added together (design brief: `docs/backup-server-brief.md`):

1. **`data/raw/` is a git repository.** Every successful ingest — a session,
   a readiness event, a scale weigh-in, an ECG recording, or a repo file —
   makes exactly one commit. Any prior state, and any accidentally deleted
   file, is recoverable from history.
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
7. Tolerant parse into the `exercises` / `routines` SQL view — any parse
   error is logged and recorded in `parse_failures`, never fails the request.
8. One git commit, `repo:1`.
9. `200 {"relPath": "...", "status": "stored"}`.

### `op: "delete"`

No `file` part. Sent when an exercise/routine is deleted, or a rename made
the old `{slug}-{id}.md` path obsolete.

- File absent → `200 {"status":"already_absent"}`, no commit.
- **Soft delete** — never `rm`. `git mv` into
  `data/raw/deleted/<relPath>.<unix_ts>`, mark the SQL row `deleted_at`
  (row is never dropped), one commit `repo:1 (delete … → deleted/)`.
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

Computed per request by walking `data/raw/` and hashing each file — fast
enough for a few hundred small files.

### `GET /v1/file?relPath=<relPath>`

Raw bytes of one live file, `Content-Type: text/markdown` for `.md` (else
`application/octet-stream`), plus an `X-Content-SHA256: sha256:…` header.
`404` if it isn't a live file, `422` if `relPath` fails the safety check.
Unlike `/v1/repo` this accepts any live subtree (sessions/readiness/scale/
ecg included) — a restore pulls everything, not just repo files. Pull-only:
the phone never asks the server to delete anything during a restore.

## The git repo

- Initialized on startup if `data/raw/.git` is missing (`git init` +
  `user.name`/`user.email`/`commit.gpgsign=false`, then a first
  "import existing store" commit if there are already files). Idempotent.
- One commit per successful request. Never batched — a commit is the
  durability boundary. Commit message:
  `sync <ISO8601 UTC> — sessions:N readiness:N scale:N ecg:N repo:N`
  (counts of files touched *in that request*).
- A duplicate delivery or an already-absent delete writes nothing, so
  `git add -A` stages nothing and no commit is made.
- If `git commit` itself fails (disk full, corrupt index) the request is
  `500` — the raw file is on disk but uncommitted; the next successful
  push's `git add -A` sweeps it in. Logged loudly.
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
