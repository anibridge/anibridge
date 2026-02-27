"""Tests for mapping range helpers."""

import pytest

from anibridge.app.utils.mapping_ranges import (
    SourceRange,
    is_valid_source_range,
    is_valid_target_range,
    parse_source_range,
    parse_source_ranges,
)


def test_is_valid_source_range_accepts_and_rejects() -> None:
    """Source range validation should accept valid patterns."""
    assert is_valid_source_range("1")
    assert is_valid_source_range("1-12")
    assert is_valid_source_range("1-")
    assert is_valid_source_range("1|2")
    assert not is_valid_source_range("a")


def test_is_valid_target_range_accepts_and_rejects() -> None:
    """Target range validation should accept comma-separated values."""
    assert is_valid_target_range("1")
    assert is_valid_target_range("1-12")
    assert is_valid_target_range("1-12,13")
    assert is_valid_target_range("1|2,3|1")
    assert not is_valid_target_range("1,,2")


def test_parse_source_range_parses_ranges() -> None:
    """Parsing should return correct start/end/ratio values."""
    parsed = parse_source_range("1-3|2")

    assert parsed == SourceRange(start=1, end=3, ratio=2)
    assert parsed.contains(2) is True
    assert parsed.contains(4) is False


def test_parse_source_range_open_end() -> None:
    """Open-ended ranges should yield None for end."""
    parsed = parse_source_range("5-")
    assert parsed.start == 5
    assert parsed.end is None


def test_parse_source_range_rejects_invalid() -> None:
    """Invalid ranges should raise a ValueError."""
    with pytest.raises(ValueError):
        parse_source_range("bad")


def test_parse_source_ranges_parses_list() -> None:
    """Parsing a list should return a tuple of SourceRange values."""
    parsed = parse_source_ranges(["1", "2-3"])

    assert len(parsed) == 2
    assert parsed[0].start == 1
    assert parsed[1].end == 3
