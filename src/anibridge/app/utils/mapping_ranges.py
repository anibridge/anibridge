"""Utilities for validating mapping range strings."""

import re
from dataclasses import dataclass

__all__ = [
    "SOURCE_RANGE_RE",
    "TARGET_RANGE_RE",
    "SourceRange",
    "is_valid_source_range",
    "is_valid_target_range",
    "parse_source_range",
    "parse_source_ranges",
]


SOURCE_RANGE_RE = re.compile(r"^\d+(?:-\d*)?(?:\|-?\d+)?$")
TARGET_RANGE_RE = re.compile(
    r"^\d+(?:-\d*)?(?:\|-?\d+)?(?:,\d+(?:-\d*)?(?:\|-?\d+)?)*$"
)


@dataclass(frozen=True, slots=True)
class SourceRange:
    """Normalized representation of a source range string."""

    start: int
    end: int | None
    ratio: int | None = None

    def contains(self, value: int) -> bool:
        """Return True when the value is within this source range."""
        if value < self.start:
            return False
        return not (self.end is not None and value > self.end)


def is_valid_source_range(value: str) -> bool:
    """Return True if the value matches the source range schema."""
    return bool(SOURCE_RANGE_RE.match(value.strip()))


def is_valid_target_range(value: str) -> bool:
    """Return True if the value matches the target range schema."""
    return bool(TARGET_RANGE_RE.match(value.strip()))


def parse_source_range(value: str) -> SourceRange:
    """Parse a source range string into a SourceRange object."""
    raw = value.strip()
    if not is_valid_source_range(raw):
        raise ValueError(f"Invalid source range '{value}'")

    range_part, _, ratio_part = raw.partition("|")
    ratio = int(ratio_part) if ratio_part else None

    if "-" in range_part:
        start_str, end_str = range_part.split("-", 1)
        start = int(start_str)
        end = int(end_str) if end_str else None
    else:
        start = int(range_part)
        end = start

    return SourceRange(start=start, end=end, ratio=ratio)


def parse_source_ranges(values: list[str] | tuple[str, ...]) -> tuple[SourceRange, ...]:
    """Parse a list of source range strings into SourceRange objects."""
    return tuple(parse_source_range(value) for value in values)
