"""docs/backup-server-brief.md Task 4: parse_exercise_file / parse_routine_file
tolerance rules (unknown keys ignored, missing optionals -> None, only `id`
required)."""

import pytest

from app.parsing import SessionParseError, parse_exercise_file, parse_routine_file
from tests.conftest import EXERCISE_MD, ROUTINE_MD


def test_parses_exercise_example() -> None:
    parsed = parse_exercise_file(EXERCISE_MD.encode())
    assert parsed.id == "ex-a1b2c3d4"
    assert parsed.name == "Bench Press"
    assert parsed.type == "FORZA"
    assert parsed.default_rep_min == 8
    assert parsed.default_rep_max == 12
    assert parsed.is_bodyweight is False
    assert parsed.bw_load_percent is None
    assert "retract scapula" in parsed.notes


def test_bodyweight_exercise_keeps_load_percent() -> None:
    md = (
        b"---\nid: ex-00000001\nname: Plank\ntype: FORZA\n"
        b"isBodyweight: true\nbwLoadPercent: 75\n---\n"
    )
    parsed = parse_exercise_file(md)
    assert parsed.is_bodyweight is True
    assert parsed.bw_load_percent == 75


def test_exercise_unknown_keys_ignored_and_minimal_file_ok() -> None:
    md = b"---\nid: ex-00000002\nsomeFutureField: 42\n---\n"
    parsed = parse_exercise_file(md)
    assert parsed.id == "ex-00000002"
    assert parsed.name is None
    assert parsed.default_rep_min is None


def test_exercise_missing_id_raises() -> None:
    with pytest.raises(SessionParseError):
        parse_exercise_file(b"---\nname: No Id\n---\n")


def test_parses_routine_example_with_child_list() -> None:
    parsed = parse_routine_file(ROUTINE_MD.encode())
    assert parsed.id == "rt-b2c3d4e5"
    assert parsed.day == "monday"
    assert parsed.enabled is True
    assert len(parsed.exercises) == 2

    first = parsed.exercises[0]
    assert first.position == 0
    assert first.exercise_id == "ex-a1b2c3d4"
    assert first.sets == 4
    assert first.is_warmup is True
    assert first.superset_with_next is False

    second = parsed.exercises[1]
    assert second.position == 1
    assert second.time_per_set_seconds is None  # omitted -> None, not 0


def test_routine_with_no_exercises_list() -> None:
    md = b"---\nid: rt-fixeddaily\nname: Fixed daily exercise\nday: ''\n---\n"
    parsed = parse_routine_file(md)
    assert parsed.exercises == []


def test_routine_all_digit_exercise_id_stays_string() -> None:
    md = b"---\nid: rt-00000001\nexercises:\n  - exerciseId: '00000123'\n    sets: 3\n---\n"
    parsed = parse_routine_file(md)
    assert parsed.exercises[0].exercise_id == "00000123"
