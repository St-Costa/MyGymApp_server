"""Covers the spec's checklist item: 'Run reparse.py --all against a
database with a couple of stored sessions and confirm it reproduces the
same rows without needing any network access.'"""

import importlib
import sqlite3
import sys

from tests.conftest import post_session


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
