"""Repo-file ingestion + restore: `POST /v1/repo`, `GET /v1/manifest`,
`GET /v1/file` (docs/backup-server-brief.md Tasks 2 & 3).

The fifth record type. Unlike the other four this one is *bidirectional* —
the phone pushes `exercises/*.md` / `routines/*.md` up (upsert/delete), and
pulls them back down during a "Ripristina dal server" restore (manifest +
per-file fetch). It rides the same bearer auth and the same raw-first /
tolerant-parse philosophy.

docs/backup/OPTIMIZATION.md points 4/5/6 changed three internals here
(the wire contract is untouched):

- Point 5: the soft-delete moves the file with `os.rename` into
  `deleted/<relPath>.<unix_ts>` instead of forking `git mv` — the debounced
  committer's `git add -A` records the move.
- Point 6: `ingest_repo_upsert` no longer parses. It fsyncs the bytes and
  returns; the route enqueues the relPath for app.repo_worker, which calls
  `parse_repo_file_into_view` off the request path.
- Point 4: `build_manifest()` caches its result keyed on `git rev-parse
  HEAD` plus a write-generation counter, so a manifest with no writes since
  the last call is O(1) instead of an os.walk + sha256 of every file.

Kept out of app/ingestion.py because it doesn't fit `ingest_record`'s
shape: two ops (upsert/delete), a soft-delete-to-tombstone path, a strict
path-traversal check, and the `_`-prefixed-basename skip for the phone's
debug test file. app/routes.py stays a thin HTTP adapter over this.
"""

import gzip
import hashlib
import io
import logging
import os
import sqlite3
import subprocess
import tarfile
import threading
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import RAW_DIR
from app.parsing import SessionParseError, parse_exercise_file, parse_routine_file
from app.storage import (
    mark_repo_file_deleted,
    record_parse_failure,
    upsert_parsed_exercise,
    upsert_parsed_routine,
)

logger = logging.getLogger("mygymapp.repo")

# The two subtrees a repo-file relPath is allowed to live under. Anything
# else — or any path escaping these via `..`, an absolute path, a symlink —
# is a 422 (see validate_rel_path). This is the one endpoint where a
# malicious/buggy client could otherwise write an arbitrary path.
_ALLOWED_PREFIXES = ("exercises/", "routines/")

# Files whose basename starts with this are written + committed like any
# other, but never produce an exercises/routines SQL row (the phone's
# routines/_debug-backup-{ts}.md "Test backup verso il server" file — see
# the brief §2.1 / §4).
_SKIP_ROW_PREFIX = "_"


class RepoPathError(ValueError):
    """relPath failed the safety check (§2.1) — traversal, absolute, or
    outside exercises/ | routines/. The route turns this into a 422."""


@dataclass
class RepoResult:
    rel_path: str
    status: str  # "stored" | "duplicate" | "deleted" | "already_absent"


# Bumped on every write to the raw store (upsert that actually wrote, or a
# delete that actually moved a file). build_manifest() compares this plus
# `git rev-parse HEAD` against what it last hashed; if neither moved, it
# returns the cached manifest instead of re-walking the tree. See
# docs/backup/OPTIMIZATION.md point 4.
_write_generation = 0
_write_gen_lock = threading.Lock()


def _bump_write_generation() -> None:
    global _write_generation
    with _write_gen_lock:
        _write_generation += 1


def _current_write_generation() -> int:
    with _write_gen_lock:
        return _write_generation


def validate_rel_path(rel_path: str) -> Path:
    """§2.1 safety check. Rejects anything that isn't a plain relative path
    under `data/raw/{exercises,routines}/`. Returns the resolved absolute
    path on success, raises RepoPathError otherwise.

    Checks, in order: non-empty; no NUL; not absolute; no `..` component;
    starts with an allowed prefix; and — belt and braces — the fully
    resolved path is still inside RAW_DIR (catches symlink trickery and any
    normalization corner case the component check misses)."""
    if not rel_path or "\x00" in rel_path:
        raise RepoPathError("relPath is empty or contains a NUL byte")

    pure = Path(rel_path)
    if pure.is_absolute() or rel_path.startswith("/"):
        raise RepoPathError(f"relPath must be relative: {rel_path!r}")
    if ".." in pure.parts:
        raise RepoPathError(f"relPath must not contain '..': {rel_path!r}")
    if not rel_path.startswith(_ALLOWED_PREFIXES):
        raise RepoPathError(f"relPath must be under exercises/ or routines/: {rel_path!r}")

    raw_root = RAW_DIR.resolve()
    resolved = (raw_root / pure).resolve()
    if resolved != raw_root and raw_root not in resolved.parents:
        raise RepoPathError(f"relPath resolves outside the raw store: {rel_path!r}")
    return resolved


def _sha256_hex(raw_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


def _table_and_parser_for(rel_path: str):
    """(table_name, parse_fn, upsert_fn) for a repo relPath, by its subtree.
    validate_rel_path has already guaranteed one of the two prefixes."""
    if rel_path.startswith("exercises/"):
        return "exercises", parse_exercise_file, upsert_parsed_exercise
    return "routines", parse_routine_file, upsert_parsed_routine


def _is_skipped_basename(rel_path: str) -> bool:
    return Path(rel_path).name.startswith(_SKIP_ROW_PREFIX)


def parse_repo_file_into_view(
    conn: sqlite3.Connection,
    *,
    rel_path: str,
    content_hash: str,
    op: str,
    received_at: str,
) -> None:
    """Worker entry point (app.repo_worker), off the request path — see
    docs/backup/OPTIMIZATION.md point 6. Re-reads the raw file from disk and
    folds it into the exercises/routines SQL view:

    - op "upsert": tolerant parse -> upsert the row (or record_parse_failure
      on a bad shape). If the file has since vanished (a delete raced ahead),
      no-op.
    - op "delete": mark the matching row `deleted_at`, don't drop it.

    Any parse error is caught and logged — the raw file + git history are
    the durable copy, and `reparse.py --stale` can always rebuild this view.
    `_`-prefixed basenames never produce a row (brief §4)."""
    if _is_skipped_basename(rel_path):
        logger.debug("skipping SQL view for _-prefixed repo file: %s", rel_path)
        return

    table, parse_fn, upsert_fn = _table_and_parser_for(rel_path)

    if op == "delete":
        mark_repo_file_deleted(
            conn, table=table, record_id=_slug_id_from(rel_path), deleted_at=received_at
        )
        return

    abs_path = RAW_DIR / rel_path
    if not abs_path.is_file():
        # A delete for this relPath landed between the upsert's fsync and
        # this parse. The delete's own worker item will mark the row; nothing
        # to do here.
        logger.info("repo file %s gone before parse (delete raced ahead) — skipping", rel_path)
        return

    raw_bytes = abs_path.read_bytes()
    try:
        parsed = parse_fn(raw_bytes)
        upsert_fn(
            conn,
            parsed,
            content_hash=content_hash,
            raw_path=rel_path,
            received_at=received_at,
        )
    except SessionParseError:
        logger.exception("failed to parse repo file %s", rel_path)
        record_parse_failure(
            conn,
            table=table,
            record_type=table,
            record_id=_fallback_record_id(rel_path),
            content_hash=content_hash,
            raw_path=rel_path,
            received_at=received_at,
            error=traceback.format_exc(),
        )


def _fallback_record_id(rel_path: str) -> str:
    """A parse failure has no parsed `id` to key parse_failures / the
    bare-minimum row by. The filename is `{slug}-{id}.md`, so the stem is
    the best stable identifier available — good enough for a human chasing
    the failure via `reparse.py`."""
    return Path(rel_path).stem


def _atomic_write(dest_abs: Path, raw_bytes: bytes) -> None:
    """§2.2 step 6: write to a temp file in the same dir, fsync, os.replace.
    Same durability guarantee as app.ingestion.write_raw_file, kept separate
    because that one takes a relPath and re-derives the target; here the
    caller already has the validated absolute path."""
    dest_abs.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_abs.with_name(f".{dest_abs.name}.tmp")
    with open(tmp, "wb") as f:
        f.write(raw_bytes)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dest_abs)


def ingest_repo_upsert(
    *,
    rel_path: str,
    content_hash: str,
    raw_bytes: bytes,
) -> RepoResult:
    """§2.2 steps 3-6. Caller (app/routes.py) has already done bearer +
    envelope validation. This does the relPath safety check, hash verify,
    on-disk idempotency check, and the atomic write. It does NOT parse or
    commit: the route enqueues the parse (app.repo_worker) and the debounced
    committer (app.git_backup) picks up the write — see
    docs/backup/OPTIMIZATION.md points 1 & 6.

    Returns "duplicate" (no write) if the file is already there byte-for-byte,
    "stored" otherwise."""
    dest_abs = validate_rel_path(rel_path)

    actual = _sha256_hex(raw_bytes)
    if actual != content_hash:
        raise ValueError(f"contentHash mismatch: envelope said {content_hash}, computed {actual}")

    # §2.2 step 5: idempotency by on-disk bytes. If the file is already there
    # and identical, no write — the route returns "duplicate", no commit,
    # no parse enqueued.
    if dest_abs.exists() and _sha256_hex(dest_abs.read_bytes()) == content_hash:
        return RepoResult(rel_path=rel_path, status="duplicate")

    _atomic_write(dest_abs, raw_bytes)
    _bump_write_generation()  # invalidate the manifest cache (point 4)
    return RepoResult(rel_path=rel_path, status="stored")


def ingest_repo_delete(*, rel_path: str) -> RepoResult:
    """§2.3. Soft delete: never `rm`. If the file exists, move it with
    `os.rename` into `deleted/<relPath>.<unix_ts>` (no `git mv` fork — see
    docs/backup/OPTIMIZATION.md point 5; the debounced committer's
    `git add -A` records the move). Returns "already_absent" (no commit,
    no parse) if there's nothing there. Does NOT touch the SQL view — the
    route enqueues that (app.repo_worker marks the row `deleted_at`)."""
    dest_abs = validate_rel_path(rel_path)

    if not dest_abs.exists():
        return RepoResult(rel_path=rel_path, status="already_absent")

    unix_ts = int(datetime.now(UTC).timestamp())
    tombstone_abs = RAW_DIR / "deleted" / f"{rel_path}.{unix_ts}"
    tombstone_abs.parent.mkdir(parents=True, exist_ok=True)
    os.rename(dest_abs, tombstone_abs)
    _bump_write_generation()

    return RepoResult(rel_path=rel_path, status="deleted")


# ---------------------------------------------------------------------------
# Batch write — POST /v1/repo/bulk (docs/backup/OPTIMIZATION.md point 2)
# ---------------------------------------------------------------------------

# Soft caps per bulk request (docs/backup/OPTIMIZATION.md point 2). Over
# either -> 413; the client splits into smaller batches. Protects the
# server's RAM (multipart is buffered) without a hard architectural limit.
BULK_MAX_FILES = 500
BULK_MAX_TOTAL_BYTES = 50 * 1024 * 1024


@dataclass
class BulkEntryResult:
    rel_path: str
    # "stored" | "duplicate" | "deleted" | "already_absent" | "error"
    status: str
    error: str | None = None


def ingest_repo_bulk(
    entries: list[dict],
    files_by_index: dict[int, bytes],
) -> tuple[list[BulkEntryResult], int, list[str]]:
    """N repo upserts/deletes in one request. `entries[i]` is
    `{relPath, op, contentHash}`; `files_by_index[i]` is the raw bytes for
    entry i when its op is "upsert". Applies every entry independently — a
    bad hash or path on one is a per-entry "error", not a request failure —
    then returns:

      (per-entry results in order, count that actually touched the tree,
       delete-tombstone notes for the commit message)

    The route makes ONE debounced commit and enqueues the parse for each
    touched entry. No commit here (same split of responsibilities as the
    single-file path)."""
    results: list[BulkEntryResult] = []
    touched = 0
    notes: list[str] = []

    for i, entry in enumerate(entries):
        rel_path = entry.get("relPath")
        op = entry.get("op")
        content_hash = entry.get("contentHash")

        if not rel_path or not op or not content_hash:
            results.append(
                BulkEntryResult(
                    rel_path=rel_path or f"<entry {i}>",
                    status="error",
                    error="entry missing relPath / op / contentHash",
                )
            )
            continue
        if op not in ("upsert", "delete"):
            results.append(
                BulkEntryResult(rel_path=rel_path, status="error", error=f"unknown op: {op!r}")
            )
            continue

        try:
            if op == "upsert":
                raw_bytes = files_by_index.get(i)
                if raw_bytes is None:
                    raise ValueError(f"no file part for upsert entry {i} ({rel_path})")
                res = ingest_repo_upsert(
                    rel_path=rel_path, content_hash=content_hash, raw_bytes=raw_bytes
                )
            else:
                res = ingest_repo_delete(rel_path=rel_path)
        except (RepoPathError, ValueError) as exc:
            results.append(BulkEntryResult(rel_path=rel_path, status="error", error=str(exc)))
            continue
        except OSError as exc:
            results.append(
                BulkEntryResult(rel_path=rel_path, status="error", error=f"write failed: {exc}")
            )
            continue

        results.append(BulkEntryResult(rel_path=res.rel_path, status=res.status))
        if res.status in ("stored", "deleted"):
            touched += 1
            from app.repo_worker import enqueue_parse

            enqueue_parse(rel_path, content_hash, op)
            if res.status == "deleted":
                notes.append(f"delete {rel_path} → deleted/")

    return results, touched, notes


# ---------------------------------------------------------------------------
# Batch read — POST /v1/repo/files, GET /v1/repo/tarball
# (docs/backup/OPTIMIZATION.md point 3)
# ---------------------------------------------------------------------------


def read_live_files(rel_paths: list[str]) -> list[tuple[str, bytes | None, str | None]]:
    """POST /v1/repo/files: for each requested relPath, return
    `(relPath, raw_bytes, sha256)` for a live file, or `(relPath, None, None)`
    if it isn't one (missing, a directory, under deleted/ | .git/, or fails
    the safety check). One list, same order as the request — the client
    diffs it against its ledger. Pull-only; never deletes anything."""
    out: list[tuple[str, bytes | None, str | None]] = []
    for rel_path in rel_paths:
        try:
            raw_bytes, sha = read_live_file(rel_path)
        except (RepoPathError, FileNotFoundError):
            out.append((rel_path, None, None))
        else:
            out.append((rel_path, raw_bytes, sha))
    return out


def build_tarball(since_manifest_hash: str | None = None) -> tuple[bytes, str]:
    """GET /v1/repo/tarball: a gzip-compressed tar of live files, for a
    from-scratch restore in one transfer. Returns `(tar_gz_bytes,
    manifest_hash)` — `manifest_hash` is the sha256 of the current manifest's
    sorted `relPath\\ncontentHash` lines, which the client passes back as
    `since=` next time to get only what changed.

    `since_manifest_hash` — when it matches the current manifest hash, the
    tar is empty (nothing changed). Otherwise every live file is included
    (a cheap, correct "changed set" — a true delta would need the client's
    old manifest, which it doesn't send here; `GET /v1/manifest` already
    covers fine-grained per-file diffing)."""
    manifest = build_manifest()["files"]
    manifest_hash = _sha256_hex(
        "\n".join(f"{p}\n{h}" for p, h in sorted(manifest.items())).encode()
    )

    raw_root = RAW_DIR.resolve()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:  # type: ignore[arg-type]
            if since_manifest_hash != manifest_hash:
                for rel_path in manifest:
                    abs_path = raw_root / rel_path
                    if abs_path.is_file():
                        tar.add(abs_path, arcname=rel_path)
    return buf.getvalue(), manifest_hash


def _slug_id_from(rel_path: str) -> str:
    """The `{id}` in a `{subtree}/{slug}-{id}.md` filename — `ex-XXXXXXXX` /
    `rt-XXXXXXXX`. Used to find the SQL row to tombstone on a delete. Falls
    back to the whole stem if the name doesn't have the `-id` shape (a
    hand-placed file, say) — mark_repo_file_deleted is a no-op if it doesn't
    match a row anyway."""
    stem = Path(rel_path).stem
    for prefix in ("ex-", "rt-"):
        idx = stem.rfind(prefix)
        if idx != -1:
            return stem[idx:]
    return stem


# ---------------------------------------------------------------------------
# Task 3 — restore endpoints
# ---------------------------------------------------------------------------


def _iter_live_files() -> list[Path]:
    """Every live file under `data/raw/` — everything except `deleted/` and
    `.git/`. A plain filesystem walk (the brief's alternative to
    `git ls-files`); for a few hundred small files this is fast enough to do
    per request."""
    raw_root = RAW_DIR.resolve()
    out: list[Path] = []
    for path in raw_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(raw_root)
        first = rel.parts[0]
        if first in (".git", "deleted"):
            continue
        out.append(path)
    return out


# Cached {relPath: sha256} plus the (HEAD commit, write-generation) it was
# computed at. build_manifest() reuses it when neither has moved since —
# docs/backup/OPTIMIZATION.md point 4. A backfill burst bumps the write
# generation on every file, so the first manifest after it still pays one
# full walk; every manifest after that (restore read-back, "Verifica
# backup" re-runs) is O(1).
_manifest_cache: dict[str, str] | None = None
_manifest_cache_key: tuple[str | None, int] | None = None
_manifest_lock = threading.Lock()


def _git_head() -> str | None:
    """`git rev-parse HEAD`, or None if the repo has no commits yet / git
    isn't reachable. Cheap (one fork, no tree walk)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(RAW_DIR), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _compute_manifest_files() -> dict[str, str]:
    raw_root = RAW_DIR.resolve()
    files: dict[str, str] = {}
    for path in _iter_live_files():
        rel = path.relative_to(raw_root).as_posix()
        files[rel] = _sha256_hex(path.read_bytes())
    return dict(sorted(files.items()))


def build_manifest() -> dict:
    """§3.1. Content hash of every live file, keyed by its relPath (forward
    slashes). `generatedAt` is an ISO-8601 UTC timestamp.

    Cached: if neither `git rev-parse HEAD` nor the raw-store write
    generation has changed since the last call, the cached file map is
    returned (only `generatedAt` is refreshed). See
    docs/backup/OPTIMIZATION.md point 4."""
    global _manifest_cache, _manifest_cache_key
    key = (_git_head(), _current_write_generation())
    with _manifest_lock:
        if _manifest_cache is not None and _manifest_cache_key == key:
            files = _manifest_cache
        else:
            files = _compute_manifest_files()
            _manifest_cache = files
            _manifest_cache_key = key
    return {
        "generatedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
    }


def read_live_file(rel_path: str) -> tuple[bytes, str]:
    """§3.2. Returns (raw_bytes, "sha256:…") for a live file. Raises
    RepoPathError (route -> 422) if the path fails the safety check — note
    this is looser than validate_rel_path's subtree restriction: a restore
    pulls back sessions/readiness/scale/ecg files too, not just
    exercises/routines. Raises FileNotFoundError (route -> 404) if it isn't
    a live file (missing, a directory, or under deleted/ | .git/)."""
    raw_root = RAW_DIR.resolve()
    if not rel_path or "\x00" in rel_path:
        raise RepoPathError("relPath is empty or contains a NUL byte")
    pure = Path(rel_path)
    if pure.is_absolute() or rel_path.startswith("/"):
        raise RepoPathError(f"relPath must be relative: {rel_path!r}")
    if ".." in pure.parts:
        raise RepoPathError(f"relPath must not contain '..': {rel_path!r}")

    resolved = (raw_root / pure).resolve()
    if raw_root not in resolved.parents:
        raise RepoPathError(f"relPath resolves outside the raw store: {rel_path!r}")
    if resolved.parts[len(raw_root.parts)] in (".git", "deleted"):
        raise FileNotFoundError(rel_path)
    if not resolved.is_file():
        raise FileNotFoundError(rel_path)

    raw_bytes = resolved.read_bytes()
    return raw_bytes, _sha256_hex(raw_bytes)
