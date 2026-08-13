"""Covers the "Testing checklist" items in docs/sync-ingestion/READINESS_SPEC.md."""

import sqlite3

from tests.conftest import content_hash_of, post_readiness, post_session


def _db_conn(app_client):
    import app.config

    return sqlite3.connect(app.config.DB_PATH)


def test_valid_readiness_event_is_stored(app_client) -> None:
    response = post_readiness(app_client)
    assert response.status_code == 200
    assert response.json() == {"eventId": "a1b2c3d4", "status": "stored"}

    import app.config

    raw_path = app.config.RAW_DIR / "readiness/a1b2c3d4.md"
    assert raw_path.exists()

    conn = _db_conn(app_client)
    row = conn.execute(
        """SELECT id, readiness, ln_rmssd, resting_hr, vo2max, recommendation
           FROM readiness_events WHERE id = ?""",
        ("a1b2c3d4",),
    ).fetchone()
    assert row == (
        "a1b2c3d4",
        "GOOD",
        4.30,
        65,
        45.2,
        "HRV above baseline. Good day to push intensity.",
    )


def test_readiness_steps_average_stored_from_envelope(app_client) -> None:
    """SYNC.md "Daily step average": the envelope is the primary source."""
    response = post_readiness(app_client, steps_avg_per_day=8214.5, steps_days_spanned=1)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT steps_avg_per_day, steps_days_spanned FROM readiness_events WHERE id = ?",
        ("a1b2c3d4",),
    ).fetchone()
    assert row == (8214.5, 1)


def test_readiness_steps_average_null_stays_null_not_zero(app_client) -> None:
    """The core "don't sink" requirement: no data available must stay NULL
    in the DB, never coerced to 0 (which would read as a genuinely
    sedentary day and corrupt aggregates)."""
    response = post_readiness(app_client, steps_avg_per_day=None, steps_days_spanned=None)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT steps_avg_per_day, steps_days_spanned FROM readiness_events WHERE id = ?",
        ("a1b2c3d4",),
    ).fetchone()
    assert row == (None, None)


def test_readiness_steps_average_envelope_overrides_frontmatter(app_client) -> None:
    """The envelope is the primary source over the .md frontmatter copy —
    even when they disagree, the live endpoint trusts the envelope."""
    md = (
        b'---\nid: a1b2c3d4\nmeasuredAt: "2026-08-05T07:04:10.123"\n'
        b'readiness: "GOOD"\nlnRmssd: 4.30\nrestingHr: 65\nvo2max: 45.2\n'
        b"stepsAvgPerDay: 1000.0\nstepsDaysSpanned: 4\n"
        b'recommendation: "HRV above baseline."\n---\n'
    )
    response = post_readiness(app_client, md, steps_avg_per_day=8214.5, steps_days_spanned=1)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT steps_avg_per_day, steps_days_spanned FROM readiness_events WHERE id = ?",
        ("a1b2c3d4",),
    ).fetchone()
    assert row == (8214.5, 1)


def test_readiness_steps_keys_omitted_from_envelope_falls_back_to_no_data(app_client) -> None:
    """Leniency case: a sender that omits the keys entirely (rather than the
    spec's explicit null) still degrades to "no data", not a 400/500."""
    response = post_readiness(app_client, omit_steps_keys=True)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT steps_avg_per_day, steps_days_spanned FROM readiness_events WHERE id = ?",
        ("a1b2c3d4",),
    ).fetchone()
    assert row == (None, None)


def test_duplicate_readiness_delivery_is_a_safe_noop(app_client) -> None:
    first = post_readiness(app_client)
    assert first.json()["status"] == "stored"

    second = post_readiness(app_client)
    assert second.status_code == 200
    assert second.json() == {"eventId": "a1b2c3d4", "status": "duplicate"}

    conn = _db_conn(app_client)
    count = conn.execute(
        "SELECT COUNT(*) FROM readiness_events WHERE id = ?", ("a1b2c3d4",)
    ).fetchone()[0]
    assert count == 1


def test_readiness_hash_mismatch_writes_nothing(app_client) -> None:
    response = post_readiness(app_client, content_hash="sha256:" + "0" * 64)
    assert response.status_code == 422

    conn = _db_conn(app_client)
    count = conn.execute("SELECT COUNT(*) FROM readiness_events").fetchone()[0]
    assert count == 0


def test_readiness_missing_bearer_token_is_rejected(app_client) -> None:
    response = post_readiness(app_client, headers={})
    assert response.status_code == 401


def test_readiness_wrong_bearer_token_is_rejected(app_client) -> None:
    response = post_readiness(app_client, headers={"Authorization": "Bearer wrong-secret"})
    assert response.status_code == 401


def test_readiness_id_colliding_with_session_id_does_not_cross_contaminate(app_client) -> None:
    """A readiness event and a session could coincidentally share the same
    8-hex id string — they live in independent tables/namespaces, so this
    must not cause any cross-table confusion."""
    from tests.conftest import SESSION_MD

    shared_id = "a1b2c3d4"
    session_md = SESSION_MD.replace("id: 3c4d5e6f", f"id: {shared_id}").encode()
    session_response = post_session(
        app_client,
        session_md,
        session_id=shared_id,
        content_hash=content_hash_of(session_md),
    )
    assert session_response.status_code == 200

    readiness_response = post_readiness(app_client, event_id=shared_id)
    assert readiness_response.status_code == 200
    assert readiness_response.json() == {"eventId": shared_id, "status": "stored"}

    conn = _db_conn(app_client)
    session_count = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE id = ?", (shared_id,)
    ).fetchone()[0]
    readiness_count = conn.execute(
        "SELECT COUNT(*) FROM readiness_events WHERE id = ?", (shared_id,)
    ).fetchone()[0]
    assert session_count == 1
    assert readiness_count == 1
