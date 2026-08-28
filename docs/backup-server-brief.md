# Server brief — git-versioned backup store + repo-file ingestion

> **This file is meant to be handed to the Claude Code session working on the *server*
> repo** (`~/docker/gym-app/`, the FastAPI receiver). It is kept in the app repo only so
> it's versioned alongside the phone-side design (`docs/BACKUP.md`). Copy its contents into
> a message for the server session.

---

## Context

`~/docker/gym-app/` runs a FastAPI service that receives raw files from the MyGymApp Android
app and writes them under `data/raw/`. Today it handles four record types:

- `POST /v1/sessions` → `data/raw/YYYY/MM/YYYY-MM-DD_rt-XXXX_YYYY.md`
- `POST /v1/readiness` → `data/raw/readiness/{id}.md`
- `POST /v1/scale-weighins` → `data/raw/scale/YYYY/MM/{date}.md`
- `POST /v1/ecg` → `data/raw/ecg/{sessionId}.ecg` (gzip on the wire)

Each request carries a multipart body: an `envelope` JSON part + a `file` bytes part, with
`Authorization: Bearer <shared-secret>`, and the server verifies the SHA-256 of the received
bytes against `envelope.contentHash`. Raw file write and SQLite parsing are decoupled: a
YAML shape the parser doesn't understand still returns `200` and the raw file is still
durable.

**Two goals for this change:**

1. Turn `data/raw/` into a **git repo**, committing after every successful write, so the
   backup is versioned and any prior state (or an accidentally deleted file) is recoverable.
2. Add a **fifth record type — "repo files"** (`exercises/*.md`, `routines/*.md`) via a new
   `POST /v1/repo`, plus **restore endpoints** (`GET /v1/manifest`, `GET /v1/file`) the
   phone uses to pull back missing/changed files.

Do **not** change the behaviour of the existing four endpoints except to add the git-commit
hook (Task 1). Keep the tolerant-parse philosophy everywhere.

---

## Task 1 — `data/raw/` is a git repository

### 1.1 Init

On service startup, if `data/raw/.git` does not exist:

```
git -C data/raw init
git -C data/raw config user.name  "MyGymApp Sync"
git -C data/raw config user.email "sync@localhost"
git -C data/raw config commit.gpgsign false
# .gitignore inside data/raw: ignore nothing by default; if the SQLite DB lives under
# data/raw (it should NOT — put it at data/app.db), ignore it here.
```

If `data/raw/` already has files (it does — ~130 weigh-ins, ~60 sessions, readiness, ecg),
the first commit is just "import existing store".

### 1.2 Commit after every successful write

Wrap the "write files → stage → commit" sequence in a **single process-wide lock**
(`asyncio.Lock` if the endpoints are async, or a `filelock` on `data/raw/.git/sync.lock`) so
two concurrent pushes can't corrupt the index.

After an endpoint has durably written its raw file(s):

```
git -C data/raw add -A
# if nothing staged -> skip (idempotent re-push of identical bytes)
git -C data/raw diff --cached --quiet || git -C data/raw commit -m "<msg>"
```

Commit message format:

```
sync 2026-09-04T20:11:03Z — sessions:1 readiness:0 scale:0 ecg:0 repo:2
```

i.e. `sync <ISO8601 UTC> — ` then a per-record-type count of files touched **in this
request**. For the existing endpoints that's always one type with count 1; for `/v1/repo`
a request is one file so it's `repo:1` (or `repo:1` with `deleted/` movement — still one).

Notes:

- **One commit per request.** Do not batch. A request either fully succeeds (files written
  + committed) or the client retries; a commit is the durability boundary.
- Plain `git commit`. **No** `git gc --aggressive`, no `git repack` cron. These are tiny
  text files; the repo stays small for years. Let git's automatic gc run at its defaults.
- If `git commit` itself fails (disk full, etc.), that's a `500` to the client — the raw
  file may be on disk but uncommitted; the next successful push's `git add -A` will sweep
  it in. Log loudly.
- Do **not** commit the SQLite DB. Keep it at `data/app.db`, outside `data/raw/`.

### 1.3 Optional off-box mirror

If env var `BACKUP_GIT_REMOTE` is set (e.g. a bare repo on another disk, or a private
GitHub/Gitea URL with a deploy key):

```
git -C data/raw push --quiet <remote> HEAD:main   # best-effort, after the local commit
```

Failure to push is logged, **not** fatal — the local commit is the real backup, the mirror
is redundancy. Do a `git push` at most once per commit; don't retry inline.

---

## Task 2 — new record type: repo files (`POST /v1/repo`)

### 2.1 Request

```
POST /v1/repo
Content-Type: multipart/form-data
Authorization: Bearer <shared-secret>

  part "envelope" (application/json):
    {
      "relPath":     "exercises/bench-press-ex-a1b2c3d4.md",
      "op":          "upsert",              // "upsert" | "delete"
      "contentHash": "sha256:9f8e7d6c…",    // upsert: hash of the bytes below
                                            // delete: hash of the last-known content (advisory)
      "appVersion":  "1.0-phaseNN",
      "clientSentAt":"2026-09-04T20:11:03Z"
    }
  part "file" (text/markdown):              // PRESENT only when op == "upsert"
    <raw bytes, unmodified>
```

`relPath` is always under `exercises/` or `routines/` for now. **Validate it**: reject with
`422` if it contains `..`, starts with `/`, is absolute, or resolves outside
`data/raw/{exercises,routines}/`. This is the one place a malicious/buggy client could write
arbitrary paths — be strict.

**Debug-test files** — the phone's Options screen has a "Test backup verso il server" button
that exercises this endpoint end to end without a real exercise/routine. It sends an
`upsert` immediately followed by a `delete` for a path of the form
`routines/_debug-backup-{epochMillis}.md` (leading underscore in the basename, `debug: true`
in the frontmatter). Treat it as a completely normal request — same auth, hash check, write,
soft-delete, commit. The **only** special handling: a basename starting with `_` must be
**excluded from the parsed SQL view** (§4) — don't try to upsert a `routines` row for it,
just log at debug level and skip. It still gets written to `data/raw/`, still committed, and
its `delete` still tombstones into `deleted/` normally. Expect one such tombstone per button
press; harmless, prune manually if ever wanted.

### 2.2 Handling `op: "upsert"`

1. Bearer check (constant-time) → `401` on mismatch.
2. Parse envelope, require `relPath`, `op`, `contentHash`. `422` if missing.
3. `relPath` safety check (§2.1) → `422`.
4. Read `file` bytes, compute sha256, compare to `contentHash` → `422` on mismatch.
5. Idempotency: if `data/raw/<relPath>` exists and its current sha256 == `contentHash` →
   respond `200 {"status":"duplicate"}`, **no write, no commit**.
6. Write bytes to `data/raw/<relPath>` (create parent dirs). Atomic: write to a temp file in
   the same dir, `fsync`, `os.replace`.
7. Tolerant parse for the SQL view (§4) — **catch and log any parse error, do not fail the
   request**.
8. Git commit (Task 1.2) with `repo:1`.
9. Respond `200 {"relPath": "...", "status": "stored"}`.

### 2.3 Handling `op: "delete"`

The phone sends this when an exercise/routine is deleted, or when a rename made the old
`{slug}-{id}.md` path obsolete.

1–3. As above (auth, envelope, path safety). No `file` part expected.
4. If `data/raw/<relPath>` does **not** exist → `200 {"status":"already_absent"}`, no commit.
5. **Soft delete** — never `rm`. Move it into a dated tombstone:
   ```
   dest = data/raw/deleted/<relPath>.<unix_ts>
   mkdir -p dirname(dest)
   git -C data/raw mv "<relPath>" "deleted/<relPath>.<unix_ts>"
   # (or plain os.rename + git add -A if git mv is awkward for nested new dirs)
   ```
6. Tolerant parse: mark the corresponding SQL row (if any) as deleted (`deleted_at` column),
   don't drop it.
7. Git commit with `repo:1`. Message can note the move:
   `sync … — repo:1 (delete exercises/foo-ex-….md → deleted/)`.
8. Respond `200 {"relPath": "...", "status": "deleted"}`.

### 2.4 Why soft delete

- git history already has the content (`git show <commit>:<path>`), **and**
- the working tree keeps a copy under `deleted/` for a no-git recovery, **and**
- an accidental "delete" tap on the phone is trivially undoable either way.

`deleted/` grows slowly and is never served by `/v1/manifest` or `/v1/file`. If it ever
needs trimming, that's a manual `git rm` + commit, deliberately.

---

## Task 3 — restore endpoints

Both behind the same bearer token. Used by the phone's "Ripristina dal server" flow and by
a fresh install with no local ledger.

### 3.1 `GET /v1/manifest`

Returns the content hash of **every live file** in `data/raw/` (everything except `deleted/`
and `.git/`):

```
200 OK
{
  "generatedAt": "2026-09-04T20:15:00Z",
  "files": {
    "2026/08/2026-08-28_rt-71284f58_b38ae530.md": "sha256:…",
    "readiness/af783e34.md": "sha256:…",
    "scale/2026/08/2026-08-28.md": "sha256:…",
    "ecg/b38ae530.ecg": "sha256:…",
    "exercises/bench-press-ex-a1b2c3d4.md": "sha256:…",
    "routines/pull-rt-71284f58.md": "sha256:…"
  }
}
```

Implementation: `git -C data/raw ls-files` (or a filesystem walk skipping `deleted/` and
`.git/`), hash each file. For a few hundred small files this is fast enough to compute per
request; cache it keyed on `git rev-parse HEAD` if it ever isn't.

### 3.2 `GET /v1/file?relPath=<relPath>`

```
200 OK
Content-Type: application/octet-stream   (or text/markdown for .md)
X-Content-SHA256: sha256:…
<raw bytes>

404  if relPath is not a live file
422  if relPath fails the safety check (§2.1)
```

The phone diffs the manifest against its local ledger and pulls only what's missing or
hash-mismatched. This is a `git fetch` in spirit — pull-only, the phone never asks the
server to delete anything during a restore.

---

## Task 4 — SQLite view for exercises/routines (optional, non-blocking)

Parallel to the existing `sessions` table. Tolerant parse; a failure logs and still returns
`200` and still commits (Task 1).

```sql
CREATE TABLE exercises (
  id            TEXT PRIMARY KEY,      -- ex-XXXXXXXX
  name          TEXT,
  type          TEXT,                  -- FORZA | STRETCH | CARDIO
  bodypart      TEXT,
  link          TEXT,                  -- image / YouTube URL
  default_rep_min INTEGER,
  default_rep_max INTEGER,
  is_bodyweight INTEGER,               -- 0/1
  bw_load_percent INTEGER,
  notes         TEXT,
  content_hash  TEXT NOT NULL,
  raw_path      TEXT NOT NULL,
  received_at   TEXT NOT NULL,
  deleted_at    TEXT,                  -- set when op:delete tombstoned it
  schema_version INTEGER NOT NULL
);

CREATE TABLE routines (
  id           TEXT PRIMARY KEY,       -- rt-XXXXXXXX
  name         TEXT,
  day          TEXT,
  enabled      INTEGER,                -- 0/1
  content_hash TEXT NOT NULL,
  raw_path     TEXT NOT NULL,
  received_at  TEXT NOT NULL,
  deleted_at   TEXT,
  schema_version INTEGER NOT NULL
);

CREATE TABLE routine_exercises (
  routine_id   TEXT NOT NULL REFERENCES routines(id),
  position     INTEGER NOT NULL,
  exercise_id  TEXT NOT NULL,
  sets         INTEGER,
  rep_min      INTEGER,
  rep_max      INTEGER,
  time_per_set_seconds INTEGER,
  superset_with_next INTEGER,          -- 0/1
  is_warmup    INTEGER                 -- 0/1
);
```

The YAML shapes to parse are in the app repo's `docs/STORAGE.md` §"Exercise" and §"Routine".
Reuse the same "unknown fields ignored, missing numeric fields default to 0" tolerance the
session parser already uses. Factor the parse into a shared function callable from both the
live `/v1/repo` handler and a `reparse.py --all` batch (same pattern as the sessions
`reparse.py`).

**Skip `_`-prefixed basenames.** Any `exercises/_*.md` / `routines/_*.md` (currently only the
phone's `routines/_debug-backup-{ts}.md` test file — see §2.1) is written and committed like
any other file but must **not** produce a `routines`/`exercises` row. The parse function
should early-return on `Path(rel_path).name.startswith("_")`. `reparse.py --all` applies the
same rule.

---

## Task 5 — docs

Update the server repo's README / sync-ingestion docs:

- New `POST /v1/repo` (envelope shape, `op` semantics, soft-delete behaviour).
- New `GET /v1/manifest`, `GET /v1/file`.
- `data/raw/` is now a git repo — one commit per successful push; `deleted/` holds
  tombstones; `BACKUP_GIT_REMOTE` for the optional mirror.
- How to recover a file: `git -C data/raw log --oneline -- <path>` then
  `git -C data/raw show <commit>:<path>`, or just look in `data/raw/deleted/`.

---

## Acceptance check

1. Restart the service on the existing `data/raw/` → it becomes a git repo, first commit
   "import existing store", `git log` shows 1 commit.
2. From the phone (or `curl`), `POST /v1/sessions` a new session → a second commit appears,
   message `sync … — sessions:1 …`.
3. `POST /v1/repo` an exercise (`op: upsert`) → commit `repo:1`, file at
   `data/raw/exercises/<slug>-ex-….md`.
4. `POST /v1/repo` the same exercise again, unchanged bytes → `200 duplicate`, **no new commit**.
5. `POST /v1/repo` `op: delete` for that exercise → file moves to `data/raw/deleted/…`,
   commit `repo:1 (delete …)`.
6. `GET /v1/manifest` → lists the session and other live files, **not** the deleted exercise.
7. `GET /v1/file?relPath=<the session>` → exact bytes, `X-Content-SHA256` matches.
8. Path-traversal: `GET /v1/file?relPath=../../etc/passwd` → `422`, nothing served.
9. Phone's Options → "Test backup verso il server": `POST /v1/repo` upsert then delete for
   `routines/_debug-backup-{ts}.md` → two `repo:1` commits, tombstone in `data/raw/deleted/`,
   **no** `routines` row created (skipped on the `_` prefix), button shows
   `OK — upsert: stored, delete: deleted`.
