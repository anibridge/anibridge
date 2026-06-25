"""Mapping projection helpers shared by record and event sync."""

from collections.abc import Sequence
from dataclasses import dataclass

from anibridge.utils.mappings import AnibridgeMapping, AnibridgeMappingRange

__all__ = ["MappingProjector"]


@dataclass(frozen=True, slots=True)
class MappingProjector:
    """Project source unit coordinates into target coordinates."""

    mappings: Sequence[AnibridgeMapping]

    def contains_source(self, index: int) -> bool:
        """Return whether any mapping covers a source unit index."""
        return any(mapping.source_range.contains(index) for mapping in self.mappings)

    def target_total(self, fallback: int | float | None = None) -> int | float | None:
        """Return the target coordinate total implied by mapped ranges."""
        total: float | None = 0.0
        for mapping in self.mappings:
            for target_range in mapping.target_ranges:
                if target_range.end is None:
                    return fallback
                total = max(total, float(target_range.end))
        return self._number(total)

    def target_progress(self, source_current: int | float | None) -> int | float:
        """Project aggregate source progress into target-coordinate progress."""
        current = 0.0
        source_value = max(float(source_current or 0), 0.0)
        for mapping in self.mappings:
            projected = self._mapping_progress(source_value, mapping)
            if projected is not None:
                current = max(current, projected)
        total = self.target_total()
        if total is not None:
            current = min(current, float(total))
        return self._number(current)

    def target_indices(self, source_index: int) -> tuple[int, ...]:
        """Project one discrete source unit into target unit indexes."""
        mapping = self.mapping_for_source(source_index)
        if mapping is None or mapping.target_ratio == 0:
            return ()

        offset = source_index - mapping.source_range.start
        ratio = mapping.target_ratio
        if ratio is None:
            if mapping.source_range.length == 1:
                return self._target_indices(mapping.target_ranges, 0, None)
            return self._target_indices(mapping.target_ranges, offset, 1)
        if ratio > 0:
            if (offset + 1) % ratio:
                return ()
            return self._target_indices(mapping.target_ranges, offset // ratio, 1)

        width = abs(ratio)
        return self._target_indices(mapping.target_ranges, offset * width, width)

    def mapping_for_source(self, index: int) -> AnibridgeMapping | None:
        """Return the most specific mapping covering a source unit index."""
        return min(
            (
                mapping
                for mapping in self.mappings
                if mapping.source_range.contains(index)
            ),
            key=lambda mapping: mapping.source_range.length or float("inf"),
            default=None,
        )

    def _mapping_progress(
        self,
        source_current: float,
        mapping: AnibridgeMapping,
    ) -> float | None:
        source_start = float(mapping.source_range.start)
        if source_current <= source_start - 1:
            return None

        source_end = mapping.source_range.end
        scoped_current = (
            source_current if source_end is None else min(source_current, source_end)
        )
        remaining = (scoped_current - source_start + 1) * mapping.source_weight
        if remaining <= 0:
            return None
        return self._target_coordinate(mapping.target_ranges, remaining)

    @classmethod
    def _target_coordinate(
        cls,
        ranges: Sequence[AnibridgeMappingRange],
        amount: float,
    ) -> float:
        remaining = amount
        for target_range in ranges:
            length = target_range.length
            if length is None or remaining <= length:
                return target_range.start + remaining - 1
            remaining -= length

        last = ranges[-1]
        return float(last.end if last.end is not None else last.start)

    @classmethod
    def _target_indices(
        cls,
        ranges: Sequence[AnibridgeMappingRange],
        offset: int,
        width: int | None,
    ) -> tuple[int, ...]:
        indexes: list[int] = []
        remaining_offset = offset
        remaining_width = width
        for target_range in ranges:
            length = target_range.length
            if length is not None and remaining_offset >= length:
                remaining_offset -= length
                continue

            start = target_range.start + remaining_offset
            if target_range.end is None:
                end = start if remaining_width is None else start + remaining_width - 1
            elif remaining_width is None:
                end = target_range.end
            else:
                end = min(target_range.end, start + remaining_width - 1)
            indexes.extend(range(start, end + 1))

            if remaining_width is None:
                remaining_offset = 0
                continue
            remaining_width -= end - start + 1
            if remaining_width <= 0:
                break
            remaining_offset = 0
        return tuple(indexes)

    @staticmethod
    def _number(value: float) -> int | float:
        return int(value) if value.is_integer() else value
