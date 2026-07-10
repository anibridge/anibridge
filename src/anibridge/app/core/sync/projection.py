"""Mapping projection helpers shared by record and event sync."""

from collections.abc import Sequence
from dataclasses import dataclass

from anibridge.utils.mappings import AnibridgeMapping

__all__ = ["MappingProjector"]


@dataclass(frozen=True, slots=True)
class MappingProjector:
    """Project source unit coordinates into target coordinates."""

    mappings: Sequence[AnibridgeMapping]

    def target_total(self, fallback: int | float | None = None) -> int | float | None:
        """Return the target coordinate total implied by mapped ranges."""
        total: float | None = 0.0
        for mapping in self.mappings:
            for target_range in mapping.target_ranges:
                if target_range.end is None:
                    return fallback
                total = max(total, float(target_range.end))
        return int(total) if total.is_integer() else total

    def target_progress(self, source_current: int | float | None) -> int | float:
        """Project aggregate source progress into target-coordinate progress."""
        current = 0.0
        source_value = max(float(source_current or 0), 0.0)
        for mapping in self.mappings:
            source_start = float(mapping.source_range.start)
            if source_value <= source_start - 1:
                continue

            source_end = mapping.source_range.end
            scoped_current = (
                source_value if source_end is None else min(source_value, source_end)
            )
            remaining = (scoped_current - source_start + 1) * mapping.source_weight
            if remaining <= 0:
                continue

            for target_range in mapping.target_ranges:
                length = target_range.length
                if length is None or remaining <= length:
                    current = max(current, target_range.start + remaining - 1)
                    break
                remaining -= length
            else:
                last = mapping.target_ranges[-1]
                current = max(
                    current,
                    float(last.end if last.end is not None else last.start),
                )
        total = self.target_total()
        if total is not None:
            current = min(current, float(total))
        return int(current) if current.is_integer() else current

    def target_indices(self, source_index: int) -> tuple[int, ...]:
        """Project one discrete source unit into target unit indexes."""
        mapping = min(
            (
                mapping
                for mapping in self.mappings
                if mapping.source_range.contains(source_index)
            ),
            key=lambda mapping: mapping.source_range.length or float("inf"),
            default=None,
        )
        if mapping is None or mapping.target_ratio == 0:
            return ()

        offset = source_index - mapping.source_range.start
        ratio = mapping.target_ratio
        if ratio is None:
            offset, width = (
                (0, None) if mapping.source_range.length == 1 else (offset, 1)
            )
        elif ratio > 0:
            if (offset + 1) % ratio:
                return ()
            offset, width = offset // ratio, 1
        else:
            width = abs(ratio)
            offset *= width

        indexes: list[int] = []
        for target_range in mapping.target_ranges:
            length = target_range.length
            if length is not None and offset >= length:
                offset -= length
                continue

            start = target_range.start + offset
            if target_range.end is None:
                end = start if width is None else start + width - 1
            elif width is None:
                end = target_range.end
            else:
                end = min(target_range.end, start + width - 1)
            indexes.extend(range(start, end + 1))

            if width is None:
                offset = 0
                continue
            width -= end - start + 1
            if width <= 0:
                break
            offset = 0
        return tuple(indexes)
