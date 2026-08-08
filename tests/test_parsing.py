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
