"""Covers the spec's checklist item: 'Run reparse.py --all against a
database with a couple of stored sessions and confirm it reproduces the
same rows without needing any network access.'"""

import importlib
import sqlite3
import sys

from tests.conftest import (
    build_ecg_bytes,
    content_hash_of,
    post_ecg,
    post_readiness,
    post_repo,
    post_scale_weighin,
    post_session,
    settle_backup,
)


def test_reparse_all_reproduces_same_rows(app_client):
    post_session(app_client)

    import app.config

    conn = sqlite3.connect(app.config.DB_PATH)
    before = conn.execute(
        "SELECT id, total_tonnage, notes, schema_version FROM sessions WHERE id = ?",
        ("3c4d5e6f",),
    ).fetchone()
    before_sets = conn.execute(
        "SELECT * FROM exercise_sets WHERE session_id = ? ORDER BY set_index", ("3c4d5e6f",)
    ).fetchall()
    conn.close()

    # reparse.py imports app.config at module scope, so make sure it picks up
    # the same env-driven DATA_ROOT the test fixture set up.
    sys.modules.pop("reparse", None)
    import reparse

    importlib.reload(reparse)

    exit_code = _run_reparse_all(reparse)
    assert exit_code == 0

    conn = sqlite3.connect(app.config.DB_PATH)
    after = conn.execute(
        "SELECT id, total_tonnage, notes, schema_version FROM sessions WHERE id = ?",
        ("3c4d5e6f",),
    ).fetchone()
    after_sets = conn.execute(
        "SELECT * FROM exercise_sets WHERE session_id = ? ORDER BY set_index", ("3c4d5e6f",)
    ).fetchall()
    conn.close()

    assert after == before
    assert after_sets == before_sets


def _run_reparse_all(reparse_module):
    old_argv = sys.argv
    sys.argv = ["reparse.py", "--all"]
    try:
        return reparse_module.main()
    finally:
        sys.argv = old_argv


def test_reparse_all_covers_all_four_record_types(app_client):
    """--all with no --type restricts to nothing — it must sweep sessions,
    readiness_events, scale_weighins, and ecg_recordings together."""
    post_session(app_client)
    post_readiness(app_client)
    post_scale_weighin(app_client)
    post_ecg(app_client)

    import app.config

    sys.modules.pop("reparse", None)
    import reparse

    importlib.reload(reparse)

    exit_code = _run_reparse_all(reparse)
    assert exit_code == 0

    conn = sqlite3.connect(app.config.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM readiness_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM scale_weighins").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ecg_recordings").fetchone()[0] == 1
    conn.close()


def test_reparse_all_reproduces_repo_file_rows(app_client):
    """docs/backup-server-brief.md Task 4: reparse must re-derive the
    exercises/routines SQL view from the raw files, and skip _-prefixed
    debug files exactly as the live /v1/repo path does."""
    post_repo(app_client)  # exercise
    post_repo(app_client, rel_path="routines/push-day-rt-b2c3d4e5.md", md_bytes=_routine_bytes())
    debug_md = b"---\nid: rt-deadbeef\nname: debug\ndebug: true\n---\n"
    post_repo(
        app_client,
        debug_md,
        rel_path="routines/_debug-backup-1725480000000.md",
        content_hash=content_hash_of(debug_md),
    )
    # The /v1/repo parse into the SQL view runs on a worker thread now
    # (docs/backup/OPTIMIZATION.md point 6) — let it land before snapshotting.
    settle_backup()

    import app.config

    # received_at is a fresh timestamp on every (re)parse — compare the
    # stable parsed columns only, same as test_reparse_all_reproduces_same_rows.
    ex_cols = (
        "id, name, type, bodypart, link, default_rep_min, default_rep_max, "
        "is_bodyweight, deleted_at, schema_version"
    )
    rt_cols = "id, name, day, enabled, deleted_at, schema_version"

    conn = sqlite3.connect(app.config.DB_PATH)
    before_ex = conn.execute(f"SELECT {ex_cols} FROM exercises ORDER BY id").fetchall()
    before_rt = conn.execute(f"SELECT {rt_cols} FROM routines ORDER BY id").fetchall()
    before_kids = conn.execute(
        "SELECT * FROM routine_exercises ORDER BY routine_id, position"
    ).fetchall()
    conn.close()

    sys.modules.pop("reparse", None)
    import reparse

    importlib.reload(reparse)
    assert _run_reparse_all(reparse) == 0

    conn = sqlite3.connect(app.config.DB_PATH)
    assert conn.execute(f"SELECT {ex_cols} FROM exercises ORDER BY id").fetchall() == before_ex
    assert conn.execute(f"SELECT {rt_cols} FROM routines ORDER BY id").fetchall() == before_rt
    assert (
        conn.execute("SELECT * FROM routine_exercises ORDER BY routine_id, position").fetchall()
        == before_kids
    )
    # The _-prefixed debug file was committed to raw/ but still has no row.
    assert conn.execute("SELECT COUNT(*) FROM routines").fetchone()[0] == 1
    conn.close()


def _routine_bytes() -> bytes:
    from tests.conftest import ROUTINE_MD

    return ROUTINE_MD.encode()


def test_reparse_readiness_falls_back_to_frontmatter_steps_fields(app_client):
    """reparse.py has no envelope to read from, so it must recover
    stepsAvgPerDay/stepsDaysSpanned (SYNC.md "Daily step average") from the
    .md frontmatter copy instead — the redundancy the spec calls for."""
    md = (
        b'---\nid: a1b2c3d4\nmeasuredAt: "2026-08-05T07:04:10.123"\n'
        b'readiness: "GOOD"\nlnRmssd: 4.30\nrestingHr: 65\nvo2max: 45.2\n'
        b"stepsAvgPerDay: 8214.5\nstepsDaysSpanned: 1\n"
        b'recommendation: "HRV above baseline."\n---\n'
    )
    post_readiness(app_client, md, steps_avg_per_day=8214.5, steps_days_spanned=1)

    import app.config

    sys.modules.pop("reparse", None)
    import reparse

    importlib.reload(reparse)

    exit_code = _run_reparse_all(reparse)
    assert exit_code == 0

    conn = sqlite3.connect(app.config.DB_PATH)
    row = conn.execute(
        "SELECT steps_avg_per_day, steps_days_spanned FROM readiness_events WHERE id = ?",
        ("a1b2c3d4",),
    ).fetchone()
    conn.close()
    assert row == (8214.5, 1)


def test_reparse_ecg_reproduces_same_row(app_client):
    """ECG's parse function doesn't recover an id from the bytes (unlike the
    other three) — this is the regression test that reparse.py's id-filling
    wrapper (RECORD_TYPES["ecg"]) actually round-trips correctly."""
    ecg_bytes = build_ecg_bytes(sample_rate=130)
    post_ecg(app_client, ecg_bytes)

    import app.config

    conn = sqlite3.connect(app.config.DB_PATH)
    before = conn.execute(
        "SELECT session_id, sample_rate, duration_seconds FROM ecg_recordings WHERE session_id = ?",
        ("3c4d5e6f",),
    ).fetchone()
    conn.close()
    assert before == ("3c4d5e6f", 130, 5.0)

    sys.modules.pop("reparse", None)
    import reparse

    importlib.reload(reparse)

    exit_code = _run_reparse_all(reparse)
    assert exit_code == 0

    conn = sqlite3.connect(app.config.DB_PATH)
    after = conn.execute(
        "SELECT session_id, sample_rate, duration_seconds FROM ecg_recordings WHERE session_id = ?",
        ("3c4d5e6f",),
    ).fetchone()
    conn.close()
    assert after == before
