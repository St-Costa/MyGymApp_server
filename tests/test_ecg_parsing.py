"""Unit tests for app.parsing.parse_ecg_file — the binary format described in
docs/sync-ingestion/ECG_SPEC.md's "Binary format" section. Operates on
already-decompressed bytes; gzip/hash concerns live in app.ingestion, tested
via tests/test_ecg_endpoint.py instead."""

import struct

import pytest

from app.parsing import SessionParseError, parse_ecg_file
from tests.conftest import build_ecg_bytes


def test_parses_well_formed_ecg_file() -> None:
    ecg_bytes = build_ecg_bytes(sample_rate=130, start_timestamp_ns=1_700_000_000_000_000_000)
    parsed = parse_ecg_file(ecg_bytes)
    assert parsed.sample_rate == 130
    assert parsed.start_timestamp_ns == 1_700_000_000_000_000_000
    assert parsed.duration_seconds == 5.0  # 650 samples / 130 Hz
    assert parsed.id == ""  # caller (ingestion/reparse) fills this in from envelope/record_id


def test_bad_magic_raises() -> None:
    bad = b"NOTMAGIC" + struct.pack(">i", 130) + struct.pack(">q", 0) + b"\x00" * 20
    with pytest.raises(SessionParseError):
        parse_ecg_file(bad)


def test_truncated_below_header_size_raises() -> None:
    truncated = b"MYGMECG1" + struct.pack(">i", 130)  # missing start_timestamp_ns
    with pytest.raises(SessionParseError):
        parse_ecg_file(truncated)


def test_empty_bytes_raises() -> None:
    with pytest.raises(SessionParseError):
        parse_ecg_file(b"")


def test_zero_samples_gives_zero_duration() -> None:
    header_only = build_ecg_bytes(sample_rate=130, samples=[])
    parsed = parse_ecg_file(header_only)
    assert parsed.duration_seconds == 0.0


def test_odd_trailing_byte_is_ignored_by_floor_division() -> None:
    """An Int16-per-sample stream with one stray trailing byte (e.g. a
    truncated last sample from an interrupted write) shouldn't crash the
    parser — the trailing half-sample is simply not counted."""
    ecg_bytes = build_ecg_bytes(sample_rate=130, samples=[100, 200, 300]) + b"\x01"
    parsed = parse_ecg_file(ecg_bytes)
    assert parsed.duration_seconds == 3 / 130


def test_non_positive_sample_rate_raises() -> None:
    bad = b"MYGMECG1" + struct.pack(">i", 0) + struct.pack(">q", 0) + b"\x00" * 4
    with pytest.raises(SessionParseError):
        parse_ecg_file(bad)


def test_negative_samples_round_trip_correctly() -> None:
    """Samples are signed Int16 (µV can be negative) — confirm the parser
    doesn't choke on negative values (it only reads the header, but this
    guards against an accidental unsigned-read regression that would still
    affect duration via sample count, e.g. mis-sized reads)."""
    ecg_bytes = build_ecg_bytes(sample_rate=130, samples=[-32768, 32767, -1, 0])
    parsed = parse_ecg_file(ecg_bytes)
    assert parsed.duration_seconds == 4 / 130
