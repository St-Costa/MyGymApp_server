"""Repo-file ingestion + restore: `POST /v1/repo`, `GET /v1/manifest`,
`GET /v1/file` (docs/backup-server-brief.md Tasks 2 & 3).

The fifth record type. Unlike the other four this one is *bidirectional* —
the phone pushes `exercises/*.md` / `routines/*.md` up (upsert/delete), and
pulls them back down during a "Ripristina dal server" restore (manifest +
per-file fetch). It rides the same bearer auth and the same raw-first /
tolerant-parse philosophy, and every successful push is one git commit
(app/git_backup.py), same as Tasks 1.2.

Kept out of app/ingestion.py because it doesn't fit `ingest_record`'s
shape: two ops (upsert/delete), a soft-delete-to-tombstone path, a strict
path-traversal check, and the `_`-prefixed-basename skip for the phone's
debug test file. app/routes.py stays a thin HTTP adapter over this.
"""

import hashlib
import logging
import os
import sqlite3
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


def _parse_into_sql_view(
    conn: sqlite3.Connection,
    rel_path: str,
    raw_bytes: bytes,
    *,
    content_hash: str,
    received_at: str,
) -> None:
    """§2.2 step 7: tolerant parse of a just-written repo file into the
    exercises/routines SQL view. Any parse error is caught and logged — it
    must never fail the request (the raw file + git commit are the durable
    copy). `_`-prefixed basenames are skipped entirely per the brief §4."""
    if _is_skipped_basename(rel_path):
        logger.debug("skipping SQL view for _-prefixed repo file: %s", rel_path)
        return

    table, parse_fn, upsert_fn = _table_and_parser_for(rel_path)
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
    conn: sqlite3.Connection,
    *,
    rel_path: str,
    content_hash: str,
    raw_bytes: bytes,
) -> RepoResult:
    """§2.2. Caller (app/routes.py) has already done bearer + envelope
    validation and the relPath safety check. This does: hash verify,
    idempotency check, atomic write, tolerant SQL-view parse. The git
    commit is the route's responsibility (so it owns the one-commit-per-
    request boundary and the 500-on-commit-failure)."""
    dest_abs = validate_rel_path(rel_path)

    actual = _sha256_hex(raw_bytes)
    if actual != content_hash:
        raise ValueError(f"contentHash mismatch: envelope said {content_hash}, computed {actual}")

    # §2.2 step 5: idempotency by on-disk bytes. If the file is already there
    # and identical, no write, no commit — the route returns "duplicate".
    if dest_abs.exists() and _sha256_hex(dest_abs.read_bytes()) == content_hash:
        return RepoResult(rel_path=rel_path, status="duplicate")

    _atomic_write(dest_abs, raw_bytes)

    received_at = datetime.now(UTC).isoformat()
    _parse_into_sql_view(
        conn, rel_path, raw_bytes, content_hash=content_hash, received_at=received_at
    )
    return RepoResult(rel_path=rel_path, status="stored")


def ingest_repo_delete(conn: sqlite3.Connection, *, rel_path: str) -> RepoResult:
    """§2.3. Soft delete: never `rm`. If the file exists, `git mv` it into
    `deleted/<relPath>.<unix_ts>` and mark the SQL row (if any) deleted.
    Returns "already_absent" (no commit) if there's nothing there. The git
    commit is the route's responsibility, same as the upsert path."""
    dest_abs = validate_rel_path(rel_path)

    if not dest_abs.exists():
        return RepoResult(rel_path=rel_path, status="already_absent")

    unix_ts = int(datetime.now(UTC).timestamp())
    tombstone_rel = f"deleted/{rel_path}.{unix_ts}"

    # Import here rather than at module load: git_backup pulls in the git
    # subprocess machinery, and keeping this module importable without it
    # makes the parse/path-safety logic unit-testable in isolation.
    from app.git_backup import git_mv

    git_mv(rel_path, tombstone_rel)

    if not _is_skipped_basename(rel_path):
        table, _parse_fn, _upsert_fn = _table_and_parser_for(rel_path)
        deleted_at = datetime.now(UTC).isoformat()
        mark_repo_file_deleted(
            conn, table=table, record_id=_slug_id_from(rel_path), deleted_at=deleted_at
        )

    return RepoResult(rel_path=rel_path, status="deleted")


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


def build_manifest() -> dict:
    """§3.1. Content hash of every live file, keyed by its relPath (forward
    slashes). `generatedAt` is an ISO-8601 UTC timestamp."""
    files: dict[str, str] = {}
    raw_root = RAW_DIR.resolve()
    for path in _iter_live_files():
        rel = path.relative_to(raw_root).as_posix()
        files[rel] = _sha256_hex(path.read_bytes())
    return {
        "generatedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": dict(sorted(files.items())),
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
