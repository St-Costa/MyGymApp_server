"""Covers the "Testing checklist" items in docs/sync-ingestion/SCALE_SPEC.md."""

import sqlite3

from tests.conftest import SCALE_WEIGHIN_MD, content_hash_of, post_scale_weighin


def _db_conn(app_client):
    import app.config

    return sqlite3.connect(app.config.DB_PATH)


def test_valid_weighin_is_stored(app_client) -> None:
    response = post_scale_weighin(app_client)
    assert response.status_code == 200
    assert response.json() == {"weighInId": "2026-08-05", "status": "stored"}

    import app.config

    raw_path = app.config.RAW_DIR / "2026/08/2026-08-05.md"
    assert raw_path.exists()

    conn = _db_conn(app_client)
    row = conn.execute(
        """SELECT id, date, weight_kg, bmi, body_fat_percent, lean_mass_percent
           FROM scale_weighins WHERE id = ?""",
        ("2026-08-05",),
    ).fetchone()
    assert row == ("2026-08-05", "2026-08-05", 78.4, 24.1, 16.8, 81.2)


def test_duplicate_weighin_delivery_is_a_safe_noop(app_client) -> None:
    first = post_scale_weighin(app_client)
    assert first.json()["status"] == "stored"

    second = post_scale_weighin(app_client)
    assert second.status_code == 200
    assert second.json() == {"weighInId": "2026-08-05", "status": "duplicate"}

    conn = _db_conn(app_client)
    count = conn.execute(
        "SELECT COUNT(*) FROM scale_weighins WHERE id = ?", ("2026-08-05",)
    ).fetchone()[0]
    assert count == 1


def test_same_day_reweigh_overwrites_not_duplicates(app_client) -> None:
    """The main behavior to test carefully here per the spec: a same-day
    re-weigh (different hash, same date/id) must overwrite, not duplicate —
    this is expected to happen routinely, unlike sessions/readiness."""
    first = post_scale_weighin(app_client)
    assert first.json()["status"] == "stored"

    updated_md = SCALE_WEIGHIN_MD.replace("weightKg: 78.4", "weightKg: 78.9").encode()
    second = post_scale_weighin(app_client, updated_md, content_hash=content_hash_of(updated_md))
    assert second.status_code == 200
    assert second.json()["status"] in ("updated", "stored")

    conn = _db_conn(app_client)
    rows = conn.execute(
        "SELECT id, weight_kg FROM scale_weighins WHERE id = ?", ("2026-08-05",)
    ).fetchall()
    assert rows == [("2026-08-05", 78.9)]


def test_weighin_hash_mismatch_writes_nothing(app_client) -> None:
    response = post_scale_weighin(app_client, content_hash="sha256:" + "0" * 64)
    assert response.status_code == 422

    conn = _db_conn(app_client)
    count = conn.execute("SELECT COUNT(*) FROM scale_weighins").fetchone()[0]
    assert count == 0


def test_weighin_missing_bearer_token_is_rejected(app_client) -> None:
    response = post_scale_weighin(app_client, headers={})
    assert response.status_code == 401


def test_weighin_wrong_bearer_token_is_rejected(app_client) -> None:
    response = post_scale_weighin(app_client, headers={"Authorization": "Bearer wrong-secret"})
    assert response.status_code == 401
