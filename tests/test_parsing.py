import pytest

from app.parsing import SessionParseError, parse_session_file
from tests.conftest import SESSION_MD


def test_parses_full_example_from_spec() -> None:
    parsed = parse_session_file(SESSION_MD.encode())

    assert parsed.id == "3c4d5e6f"
    assert parsed.routine_id == "rt-b2c3d4e5"
    assert parsed.total_tonnage == 12450.0
    assert parsed.tonnage_by_bodypart == {"chest": 8200.0, "shoulders": 4250.0}
    assert parsed.notes == "Optional free-text session notes go here."

    assert len(parsed.exercise_sets) == 2
    first_set = parsed.exercise_sets[0]
    assert first_set.exercise_id == "ex-a1b2c3d4"
    assert first_set.exercise_type == "FORZA"
    assert first_set.reps == 8
    assert first_set.weight == 80.0
    assert first_set.exclude_from_tonnage is True
    assert first_set.is_daily is True
    assert first_set.set_index == 0
    assert parsed.exercise_sets[1].set_index == 1


def test_empty_completed_at_means_abandoned_session() -> None:
    md = b'---\nid: aaaaaaaa\ncompletedAt: ""\ntotalTonnage: 10.0\n---\n'
    parsed = parse_session_file(md)
    assert parsed.completed_at == ""


def test_exclude_from_tonnage_and_is_daily_default_to_false_when_omitted() -> None:
    md = (
        b"---\n"
        b"id: bbbbbbbb\n"
        b"exercises:\n"
        b"  - exerciseId: ex-11111111\n"
        b"    type: STRETCH\n"
        b"    sets:\n"
        b"      - reps: 1\n"
        b"        weight: 0.0\n"
        b"---\n"
    )
    parsed = parse_session_file(md)
    assert parsed.exercise_sets[0].exclude_from_tonnage is False
    assert parsed.exercise_sets[0].is_daily is False


def test_session_rpe_defaults_to_none_when_omitted() -> None:
    # SESSION_MD (§13's new field) doesn't send sessionRpe/sessionLoad — must
    # stay None, not collapse to 0 like the other numeric session fields.
    parsed = parse_session_file(SESSION_MD.encode())
    assert parsed.session_rpe is None
    assert parsed.session_load is None


def test_session_rpe_parsed_when_present() -> None:
    md = SESSION_MD.replace("hrr60s: 28\n", "hrr60s: 28\nsessionRpe: 7\nsessionLoad: 315.0\n")
    parsed = parse_session_file(md.encode())
    assert parsed.session_rpe == 7
    assert parsed.session_load == 315.0


def test_session_rpe_zero_is_distinguishable_from_omitted() -> None:
    # Not a realistic rating (Foster's scale starts at 1), but the parser
    # must not treat an explicit 0 the same as "field absent" — this is
    # exactly the distinction _optional_int exists to preserve.
    md = SESSION_MD.replace("hrr60s: 28\n", "hrr60s: 28\nsessionRpe: 0\n")
    parsed = parse_session_file(md.encode())
    assert parsed.session_rpe == 0


def test_is_bodyweight_defaults_to_false_when_omitted() -> None:
    # isBodyweight isn't sent by the phone yet — must not error, must default False.
    md = (
        b"---\n"
        b"id: cccccccc\n"
        b"exercises:\n"
        b"  - exerciseId: ex-22222222\n"
        b"    type: FORZA\n"
        b"    sets:\n"
        b"      - reps: 12\n"
        b"        weight: 0.0\n"
        b"---\n"
    )
    parsed = parse_session_file(md)
    assert parsed.exercise_sets[0].is_bodyweight is False


def test_is_bodyweight_parsed_when_present() -> None:
    md = (
        b"---\n"
        b"id: dddddddd\n"
        b"exercises:\n"
        b"  - exerciseId: ex-33333333\n"
        b"    type: FORZA\n"
        b"    sets:\n"
        b"      - reps: 12\n"
        b"        weight: 0.0\n"
        b"        isBodyweight: true\n"
        b"---\n"
    )
    parsed = parse_session_file(md)
    assert parsed.exercise_sets[0].is_bodyweight is True


def test_bodyweight_load_audit_fields_parsed_when_present() -> None:
    # The phone now materializes `weight` for bodyweight sets and attaches the
    # audit pair explaining it. All three must round-trip.
    md = (
        b"---\n"
        b"id: eeeeeeee\n"
        b"exercises:\n"
        b"  - exerciseId: ex-44444444\n"
        b"    type: FORZA\n"
        b"    sets:\n"
        b"      - reps: 10\n"
        b"        weight: 61.5\n"
        b"        isBodyweight: true\n"
        b"        bwLoadPercent: 75\n"
        b"        bwBaseWeightKg: 82.0\n"
        b"---\n"
    )
    parsed = parse_session_file(md)
    s = parsed.exercise_sets[0]
    assert s.is_bodyweight is True
    assert s.weight == 61.5
    assert s.bw_load_percent == 75
    assert s.bw_base_weight_kg == 82.0


def test_bodyweight_load_audit_fields_default_to_none_when_omitted() -> None:
    # Older synced sessions (and every non-bodyweight set) carry neither field.
    md = (
        b"---\n"
        b"id: ffffffff\n"
        b"exercises:\n"
        b"  - exerciseId: ex-55555555\n"
        b"    type: FORZA\n"
        b"    sets:\n"
        b"      - reps: 8\n"
        b"        weight: 80.0\n"
        b"---\n"
    )
    parsed = parse_session_file(md)
    s = parsed.exercise_sets[0]
    assert s.bw_load_percent is None
    assert s.bw_base_weight_kg is None


def test_cardio_exercise_populates_cardio_fields_and_nulls_reps_weight() -> None:
    md = (
        b"---\n"
        b"id: ffffffff\n"
        b"exercises:\n"
        b"  - exerciseId: ex-c9d8e7f6\n"
        b"    exerciseName: Corsa leggera\n"
        b"    type: CARDIO\n"
        b"    bodypart: cardio\n"
        b"    excludeFromTonnage: true\n"
        b"    sets:\n"
        b'      - startedAt: "2026-08-12T10:15:03"\n'
        b'        endedAt: "2026-08-12T10:45:10"\n'
        b"        avgHr: 138\n"
        b"        maxHr: 156\n"
        b"---\n"
    )
    parsed = parse_session_file(md)
    assert len(parsed.exercise_sets) == 1
    cardio_set = parsed.exercise_sets[0]
    assert cardio_set.exercise_type == "CARDIO"
    assert cardio_set.started_at == "2026-08-12T10:15:03"
    assert cardio_set.ended_at == "2026-08-12T10:45:10"
    assert cardio_set.avg_hr == 138
    assert cardio_set.max_hr == 156
    assert cardio_set.reps is None
    assert cardio_set.weight is None
    assert cardio_set.exclude_from_tonnage is True


def test_cardio_exercise_blank_ended_at_means_block_still_running() -> None:
    # Per CARDIO_BLOCKS_SPEC.md: endedAt: "" means the block is still
    # running/abandoned — must land as None/NULL, not "", since "" is itself
    # a distinct (if degenerate) value elsewhere. avgHr/maxHr: 0 in this same
    # example are literal on-device-computed values (however degenerate for
    # an unfinished block) and parse as the int 0, not None — 0 is a
    # meaningfully different case from "field omitted entirely".
    md = (
        b"---\n"
        b"id: 11111112\n"
        b"exercises:\n"
        b"  - exerciseId: ex-c9d8e7f6\n"
        b"    type: CARDIO\n"
        b"    sets:\n"
        b'      - startedAt: "2026-08-12T10:50:00"\n'
        b'        endedAt: ""\n'
        b"        avgHr: 0\n"
        b"        maxHr: 0\n"
        b"---\n"
    )
    parsed = parse_session_file(md)
    cardio_set = parsed.exercise_sets[0]
    assert cardio_set.started_at == "2026-08-12T10:50:00"
    assert cardio_set.ended_at is None
    assert cardio_set.avg_hr == 0
    assert cardio_set.max_hr == 0


def test_cardio_exercise_omitted_hr_fields_stay_none() -> None:
    # avgHr/maxHr entirely absent from the YAML (not even present as 0) must
    # stay None — distinguishable from an explicit 0 reading, same
    # "_optional_int, not _int" convention as sessionRpe.
    md = (
        b"---\n"
        b"id: 11111113\n"
        b"exercises:\n"
        b"  - exerciseId: ex-c9d8e7f6\n"
        b"    type: CARDIO\n"
        b"    sets:\n"
        b'      - startedAt: "2026-08-12T10:50:00"\n'
        b'        endedAt: ""\n'
        b"---\n"
    )
    parsed = parse_session_file(md)
    cardio_set = parsed.exercise_sets[0]
    assert cardio_set.avg_hr is None
    assert cardio_set.max_hr is None


def test_non_cardio_exercise_leaves_cardio_fields_none() -> None:
    parsed = parse_session_file(SESSION_MD.encode())
    forza_set = parsed.exercise_sets[0]
    assert forza_set.started_at is None
    assert forza_set.ended_at is None
    assert forza_set.avg_hr is None
    assert forza_set.max_hr is None


def test_stretch_exercise_populates_time_seconds_and_nulls_reps_weight() -> None:
    # A STRETCH set is {timeSeconds, done} — regression for the bug where
    # STRETCH ran through the FORZA path, forcing reps/weight to 0 and
    # dropping timeSeconds entirely.
    md = (
        b"---\n"
        b"id: 11111114\n"
        b"exercises:\n"
        b"  - exerciseId: ex-aabbccdd\n"
        b"    type: STRETCH\n"
        b"    bodypart: hamstrings\n"
        b"    sets:\n"
        b"      - timeSeconds: 45\n"
        b"        done: true\n"
        b"      - timeSeconds: 60\n"
        b"        done: true\n"
        b"---\n"
    )
    parsed = parse_session_file(md)
    assert len(parsed.exercise_sets) == 2
    first = parsed.exercise_sets[0]
    assert first.exercise_type == "STRETCH"
    assert first.time_seconds == 45
    assert first.reps is None
    assert first.weight is None
    assert parsed.exercise_sets[1].time_seconds == 60


def test_stretch_set_without_time_seconds_stays_none() -> None:
    # An untouched/abandoned stretch set (no timeSeconds) must stay None, not
    # collapse to 0 — same _optional_int convention as the CARDIO HR fields.
    md = (
        b"---\n"
        b"id: 11111115\n"
        b"exercises:\n"
        b"  - exerciseId: ex-eeff0011\n"
        b"    type: STRETCH\n"
        b"    sets:\n"
        b"      - done: false\n"
        b"---\n"
    )
    parsed = parse_session_file(md)
    assert parsed.exercise_sets[0].time_seconds is None


def test_non_stretch_exercise_leaves_time_seconds_none() -> None:
    parsed = parse_session_file(SESSION_MD.encode())
    assert parsed.exercise_sets[0].time_seconds is None


def test_cardio_exercise_matches_type_case_insensitively() -> None:
    # Regression: real synced data has been observed sending lowercase
    # `type: cardio`, not SPEC.md's documented `CARDIO` — a case-sensitive
    # match would silently misparse this as a FORZA exercise (reps/weight
    # forced to 0, startedAt/endedAt/avgHr/maxHr dropped) instead of erroring.
    md = (
        b"---\n"
        b"id: ffffffff\n"
        b"exercises:\n"
        b"  - exerciseId: ex-c9d8e7f6\n"
        b"    type: cardio\n"
        b"    sets:\n"
        b'      - startedAt: "2026-08-12T10:15:03"\n'
        b'        endedAt: "2026-08-12T10:45:10"\n'
        b"        avgHr: 138\n"
        b"        maxHr: 156\n"
        b"---\n"
    )
    parsed = parse_session_file(md)
    cardio_set = parsed.exercise_sets[0]
    assert cardio_set.started_at == "2026-08-12T10:15:03"
    assert cardio_set.avg_hr == 138
    assert cardio_set.reps is None
    assert cardio_set.weight is None


def test_unknown_top_level_keys_are_ignored() -> None:
    md = b"---\nid: cccccccc\ntotalTonnage: 1.0\nsomeFutureField: whatever\n---\n"
    parsed = parse_session_file(md)
    assert parsed.id == "cccccccc"


def test_missing_frontmatter_delimiter_raises() -> None:
    with pytest.raises(SessionParseError):
        parse_session_file(b"no frontmatter here at all")


def test_unclosed_frontmatter_raises() -> None:
    with pytest.raises(SessionParseError):
        parse_session_file(b"---\nid: dddddddd\n")


def test_missing_id_raises() -> None:
    md = b"---\ntotalTonnage: 1.0\n---\n"
    with pytest.raises(SessionParseError):
        parse_session_file(md)


def test_body_can_be_empty() -> None:
    md = b"---\nid: eeeeeeee\n---\n"
    parsed = parse_session_file(md)
    assert parsed.notes == ""
