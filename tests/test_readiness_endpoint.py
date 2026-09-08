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


def test_readiness_sleep_quality_stored_from_envelope(app_client) -> None:
    """READINESS_SPEC.md: the envelope is the primary source, same as the
    steps fields — it's what's stored without re-parsing the markdown."""
    response = post_readiness(app_client, sleep_quality=3)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT sleep_quality FROM readiness_events WHERE id = ?", ("a1b2c3d4",)
    ).fetchone()
    assert row == (3,)


def test_readiness_sleep_quality_null_stays_null_not_zero(app_client) -> None:
    """ "non valutato" must stay NULL, never coerced to 0 — same rule as the
    steps fields (an explicit JSON null, not an omitted key)."""
    response = post_readiness(app_client, sleep_quality=None)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT sleep_quality FROM readiness_events WHERE id = ?", ("a1b2c3d4",)
    ).fetchone()
    assert row == (None,)


def test_readiness_sleep_quality_envelope_overrides_frontmatter(app_client) -> None:
    """The .md frontmatter carries sleepQuality too, but the live endpoint
    trusts the envelope when both are present — same as the steps fields."""
    md = (
        b'---\nid: a1b2c3d4\nmeasuredAt: "2026-08-05T07:04:10.123"\n'
        b'readiness: "GOOD"\nlnRmssd: 4.30\nrestingHr: 65\nvo2max: 45.2\n'
        b'sleepQuality: 1\nrecommendation: "HRV above baseline."\n---\n'
    )
    response = post_readiness(app_client, md, sleep_quality=5)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT sleep_quality FROM readiness_events WHERE id = ?", ("a1b2c3d4",)
    ).fetchone()
    assert row == (5,)


def test_readiness_sleep_quality_key_omitted_falls_back_to_no_rating(app_client) -> None:
    """Leniency case: a sender that omits the key entirely degrades to "non
    valutato", not a 400 — same tolerance as the rest of the envelope."""
    response = post_readiness(app_client, omit_sleep_quality_key=True)
    assert response.status_code == 200

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT sleep_quality FROM readiness_events WHERE id = ?", ("a1b2c3d4",)
    ).fetchone()
    assert row == (None,)


def test_readiness_sleep_quality_out_of_range_is_400(app_client) -> None:
    """READINESS_SPEC.md: present but neither null nor an int in [1,5] → 400,
    nothing written. Distinct from the steps fields, which are lenient."""
    for bad in (0, 6, -1, 2.5, "3", True):
        response = post_readiness(app_client, sleep_quality=bad)
        assert response.status_code == 400, f"expected 400 for sleepQuality={bad!r}"

    conn = _db_conn(app_client)
    count = conn.execute("SELECT COUNT(*) FROM readiness_events").fetchone()[0]
    assert count == 0


def test_readiness_rerated_sleep_updates_row_not_rejected_as_duplicate(app_client) -> None:
    """The critical case (READINESS_SPEC.md §handling step 4): the phone
    re-sends a known eventId with a *new* contentHash after the user rates
    sleep post-measurement (ReadinessRepository.updateSleepQuality rewrites
    the .md and re-queues it). Must be an update — new sleep_quality, new
    content_hash, overwritten archived .md — not a duplicate or a
    parse_failure."""
    unrated_md = (
        b'---\nid: a1b2c3d4\nmeasuredAt: "2026-08-05T07:04:10.123"\n'
        b'readiness: "GOOD"\nlnRmssd: 4.30\nrestingHr: 65\nvo2max: 45.2\n'
        b"sleepQuality: null\n"
        b'recommendation: "HRV above baseline. Good day to push intensity."\n---\n'
    )
    first = post_readiness(app_client, unrated_md, sleep_quality=None)
    assert first.json() == {"eventId": "a1b2c3d4", "status": "stored"}

    rerated_md = unrated_md.replace(b"sleepQuality: null", b"sleepQuality: 4")
    second = post_readiness(app_client, rerated_md, sleep_quality=4)
    assert second.status_code == 200
    assert second.json() == {"eventId": "a1b2c3d4", "status": "updated"}

    import app.config

    conn = _db_conn(app_client)
    row = conn.execute(
        "SELECT sleep_quality, content_hash FROM readiness_events WHERE id = ?",
        ("a1b2c3d4",),
    ).fetchone()
    assert row[0] == 4
    assert row[1] == content_hash_of(rerated_md)

    # Point 4: the archived raw file is overwritten with the new bytes, so
    # the raw store stays the coherent source of truth for the row.
    raw_path = app.config.RAW_DIR / "readiness/a1b2c3d4.md"
    assert raw_path.read_bytes() == rerated_md

    # Not double-counted, and no parse_failure recorded.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM readiness_events WHERE id = ?", ("a1b2c3d4",)
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM parse_failures WHERE record_id = ?", ("a1b2c3d4",)
        ).fetchone()[0]
        == 0
    )


def test_readiness_resent_same_hash_after_rating_is_still_a_duplicate(app_client) -> None:
    """The other half of step 4: a known eventId re-sent with the *same*
    contentHash (the phone just missed the first 200) stays a no-op."""
    post_readiness(app_client, sleep_quality=4)
    second = post_readiness(app_client, sleep_quality=4)
    assert second.json() == {"eventId": "a1b2c3d4", "status": "duplicate"}


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
