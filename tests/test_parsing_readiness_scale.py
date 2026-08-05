import pytest

from app.parsing import SessionParseError, parse_readiness_file, parse_scale_weighin_file
from tests.conftest import READINESS_MD, SCALE_WEIGHIN_MD


def test_parses_full_readiness_example() -> None:
    parsed = parse_readiness_file(READINESS_MD.encode())
    assert parsed.id == "a1b2c3d4"
    assert parsed.measured_at == "2026-08-05T07:04:10.123"
    assert parsed.readiness == "GOOD"
    assert parsed.ln_rmssd == 4.30
    assert parsed.resting_hr == 65
    assert parsed.vo2max == 45.2
    assert parsed.recommendation == "HRV above baseline. Good day to push intensity."


def test_readiness_missing_id_raises() -> None:
    md = b'---\nmeasuredAt: "2026-08-05T07:04:10.123"\n---\n'
    with pytest.raises(SessionParseError):
        parse_readiness_file(md)


def test_readiness_missing_measured_at_raises() -> None:
    md = b"---\nid: aaaaaaaa\n---\n"
    with pytest.raises(SessionParseError):
        parse_readiness_file(md)


def test_readiness_omitted_numeric_fields_default_to_zero() -> None:
    md = b'---\nid: aaaaaaaa\nmeasuredAt: "2026-08-05T07:00:00"\n---\n'
    parsed = parse_readiness_file(md)
    assert parsed.ln_rmssd == 0.0
    assert parsed.resting_hr == 0
    assert parsed.vo2max == 0.0
    assert parsed.recommendation == ""


def test_parses_full_scale_weighin_example() -> None:
    parsed = parse_scale_weighin_file(SCALE_WEIGHIN_MD.encode())
    assert parsed.id == "2026-08-05"
    assert parsed.date == "2026-08-05"
    assert parsed.recorded_at == "2026-08-05T07:12:34"
    assert parsed.weight_kg == 78.4
    assert parsed.bmi == 24.1
    assert parsed.body_fat_percent == 16.8
    assert parsed.lean_mass_percent == 81.2


def test_scale_weighin_id_is_iso_date_string_not_corrupted() -> None:
    # id/date values are already quoted on the wire ("2026-08-05"), but this
    # confirms the shared string-id YAML loader doesn't mangle them either way.
    md = b'---\nid: "2026-01-01"\ndate: "2026-01-01"\nweightKg: 70.0\n---\n'
    parsed = parse_scale_weighin_file(md)
    assert parsed.id == "2026-01-01"


def test_scale_weighin_missing_id_raises() -> None:
    md = b"---\nweightKg: 70.0\n---\n"
    with pytest.raises(SessionParseError):
        parse_scale_weighin_file(md)
