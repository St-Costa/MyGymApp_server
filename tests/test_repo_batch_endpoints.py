"""docs/backup/OPTIMIZATION.md points 2 & 3: POST /v1/repo/bulk, POST
/v1/repo/files, GET /v1/repo/tarball."""

import io
import json
import sqlite3
import tarfile

from tests.conftest import auth_headers, content_hash_of, post_repo, settle_backup


def _db(app_client) -> sqlite3.Connection:
    import app.config

    return sqlite3.connect(app.config.DB_PATH)


def _git_log(app_client) -> list[str]:
    import subprocess

    import app.config

    out = subprocess.run(
        ["git", "-C", str(app.config.RAW_DIR), "log", "--format=%s"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _exercise_md(idx: int, name: str = "ex") -> bytes:
    return f"---\nid: ex-{idx:08x}\nname: {name}\ntype: forza\n---\n".encode()


def _entry(rel_path: str, op: str, *, hash_of: bytes | None = None, bad_hash: bool = False) -> dict:
    ch = "sha256:" + "0" * 64 if bad_hash or hash_of is None else content_hash_of(hash_of)
    return {"relPath": rel_path, "op": op, "contentHash": ch}


def _bulk(app_client, entries, files=None):
    return app_client.post(
        "/v1/repo/bulk",
        data={"envelope": json.dumps(entries)},
        files=files or {},
        headers=auth_headers(),
    )


# --- POST /v1/repo/bulk ---------------------------------------------------


def test_bulk_upserts_land_in_one_commit(app_client) -> None:
    entries, files = [], {}
    for i in range(6):
        md = _exercise_md(i, f"e{i}")
        entries.append(_entry(f"exercises/e{i}-ex-{i:08x}.md", "upsert", hash_of=md))
        files[f"file_{i}"] = ("f.md", md, "text/markdown")

    resp = _bulk(app_client, entries, files)
    assert resp.status_code == 200
    assert [r["status"] for r in resp.json()["results"]] == ["stored"] * 6

    settle_backup()
    log = _git_log(app_client)
    assert len(log) == 1 and "repo:6" in log[0]

    # All six rows parsed into the SQL view.
    n = _db(app_client).execute("SELECT COUNT(*) FROM exercises").fetchone()[0]
    assert n == 6


def test_bulk_per_entry_error_does_not_fail_the_request(app_client) -> None:
    good = _exercise_md(0xAA, "ok")
    resp = _bulk(
        app_client,
        [
            _entry("exercises/ok-ex-000000aa.md", "upsert", hash_of=good),
            _entry("exercises/bad-ex-000000bb.md", "upsert", bad_hash=True),
            _entry("../../escape.md", "upsert", bad_hash=True),
        ],
        {
            "file_0": ("f.md", good, "text/markdown"),
            "file_1": ("f.md", b"mismatch", "text/markdown"),
            "file_2": ("f.md", b"x", "text/markdown"),
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["status"] == "stored"
    assert results[1]["status"] == "error" and "mismatch" in results[1]["error"]
    assert results[2]["status"] == "error"

    import app.config

    assert not (app.config.RAW_DIR / "escape.md").exists()
    assert not (app.config.RAW_DIR.parent / "escape.md").exists()


def test_bulk_delete_of_absent_is_already_absent(app_client) -> None:
    resp = _bulk(app_client, [_entry("exercises/ghost-ex-ffffffff.md", "delete", bad_hash=True)])
    assert resp.json()["results"] == [
        {"relPath": "exercises/ghost-ex-ffffffff.md", "status": "already_absent"}
    ]
    settle_backup()
    assert _git_log(app_client) == []  # nothing touched, no commit


def test_bulk_upsert_then_delete_in_one_batch(app_client) -> None:
    md = _exercise_md(1, "temp")
    rel = "exercises/temp-ex-00000001.md"
    resp = _bulk(
        app_client,
        [_entry(rel, "upsert", hash_of=md), _entry(rel, "delete", hash_of=md)],
        {"file_0": ("f.md", md, "text/markdown")},
    )
    assert [r["status"] for r in resp.json()["results"]] == ["stored", "deleted"]
    settle_backup()

    import app.config

    assert not (app.config.RAW_DIR / rel).exists()
    assert list((app.config.RAW_DIR / "deleted/exercises").glob("temp-ex-00000001.md.*"))


def test_bulk_rejects_too_many_entries(app_client) -> None:
    from app.repo import BULK_MAX_FILES

    entries = [
        _entry(f"exercises/x{i}-ex-{i:08x}.md", "delete", bad_hash=True)
        for i in range(BULK_MAX_FILES + 1)
    ]
    assert _bulk(app_client, entries).status_code == 413


def test_bulk_requires_bearer(app_client) -> None:
    resp = app_client.post(
        "/v1/repo/bulk", data={"envelope": "[]"}, headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status_code == 401


# --- POST /v1/repo/files -----------------------------------------------------


def _parse_multipart_mixed(content: bytes, content_type: str) -> list[dict]:
    boundary = content_type.split('boundary="')[1].rstrip('"')
    # Each part is `<headers>\r\n\r\n<body>\r\n` between `--<boundary>`
    # delimiters; strip exactly the framing \r\n, not the body's own.
    raw_parts = content.split(f"--{boundary}".encode())
    out = []
    for raw in raw_parts[1:-1]:  # drop the empty preamble and the closing `--\r\n`
        raw = raw.removeprefix(b"\r\n").removesuffix(b"\r\n")
        head, _, body = raw.partition(b"\r\n\r\n")
        headers = dict(line.split(": ", 1) for line in head.decode().split("\r\n") if ": " in line)
        out.append({"headers": headers, "body": body})
    return out


def test_repo_files_returns_present_and_absent_parts(app_client) -> None:
    assert post_repo(app_client).json()["status"] == "stored"
    settle_backup()

    resp = app_client.post(
        "/v1/repo/files",
        json={
            "relPaths": [
                "exercises/bench-press-ex-a1b2c3d4.md",
                "exercises/missing-ex-00000000.md",
            ]
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    parts = _parse_multipart_mixed(resp.content, resp.headers["content-type"])
    assert len(parts) == 2

    present, absent = parts
    assert present["headers"]["X-Status"] == "present"
    assert present["headers"]["X-Content-SHA256"] == content_hash_of(present["body"])
    disp = present["headers"]["Content-Disposition"]
    assert 'name="exercises/bench-press-ex-a1b2c3d4.md"' in disp

    assert absent["headers"]["X-Status"] == "absent"
    assert absent["body"] == b""


def test_repo_files_traversal_entry_is_absent_not_error(app_client) -> None:
    resp = app_client.post(
        "/v1/repo/files", json={"relPaths": ["../../etc/passwd"]}, headers=auth_headers()
    )
    assert resp.status_code == 200
    parts = _parse_multipart_mixed(resp.content, resp.headers["content-type"])
    assert parts[0]["headers"]["X-Status"] == "absent"


def test_repo_files_rejects_bad_body(app_client) -> None:
    h = auth_headers()
    assert app_client.post("/v1/repo/files", json={}, headers=h).status_code == 422
    assert app_client.post("/v1/repo/files", json={"relPaths": "x"}, headers=h).status_code == 422


# --- GET /v1/repo/tarball --------------------------------------------------


def test_tarball_contains_live_files_and_since_shortcircuits(app_client) -> None:
    assert post_repo(app_client).json()["status"] == "stored"
    other = _exercise_md(0x99, "squat")
    assert (
        post_repo(
            app_client,
            other,
            rel_path="exercises/squat-ex-00000099.md",
            content_hash=content_hash_of(other),
        ).json()["status"]
        == "stored"
    )
    settle_backup()

    resp = app_client.get("/v1/repo/tarball", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    manifest_hash = resp.headers["X-Manifest-SHA256"]

    tf = tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz")
    names = set(tf.getnames())
    assert "exercises/bench-press-ex-a1b2c3d4.md" in names
    assert "exercises/squat-ex-00000099.md" in names
    # Extracted bytes match what /v1/file would serve.
    member = tf.extractfile("exercises/squat-ex-00000099.md").read()
    assert content_hash_of(member) == content_hash_of(other)

    # Same manifest hash back -> empty tar.
    again = app_client.get(
        "/v1/repo/tarball", params={"since": manifest_hash}, headers=auth_headers()
    )
    tf2 = tarfile.open(fileobj=io.BytesIO(again.content), mode="r:gz")
    assert tf2.getnames() == []


def test_tarball_excludes_deleted(app_client) -> None:
    assert post_repo(app_client).json()["status"] == "stored"
    settle_backup()
    assert post_repo(app_client, md_bytes=None, op="delete").json()["status"] == "deleted"
    settle_backup()

    resp = app_client.get("/v1/repo/tarball", headers=auth_headers())
    tf = tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz")
    assert not any("bench-press" in n for n in tf.getnames())
    assert not any(n.startswith("deleted/") for n in tf.getnames())


def test_tarball_requires_bearer(app_client) -> None:
    assert app_client.get("/v1/repo/tarball").status_code == 401
