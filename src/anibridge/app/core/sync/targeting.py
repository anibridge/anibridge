"""Helpers for resolving list targets from library mappings."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from anibridge.library import LibraryEntry
from anibridge.list import ListEntry, ListProvider
from anibridge.utils.mappings import AnibridgeDescriptorMapping, descriptor_key
from anibridge.utils.types import MappingDescriptor
from rapidfuzz import fuzz

from anibridge.app.core.animap import AnimapClient
from anibridge.app.core.sync.stats import EntrySnapshot

__all__ = [
    "ResolvedListTarget",
    "SyncTarget",
    "diff_snapshots",
    "find_best_search_result",
    "resolve_list_targets",
    "resolve_list_targets_batch",
]


def diff_snapshots(
    before: EntrySnapshot | None,
    after: EntrySnapshot | None,
    fields: set[str],
) -> dict[str, tuple[Any, Any]]:
    """Compute differences between two snapshots.

    Args:
        before (EntrySnapshot | None): Snapshot captured before mutation.
        after (EntrySnapshot | None): Snapshot captured after mutation.
        fields (set[str]): Field names to compare.

    Returns:
        dict[str, tuple[Any, Any]]: Changed fields mapped to before/after values.
    """
    diff: dict[str, tuple[Any, Any]] = {}
    before_map = before.to_dict() if before else {}
    after_map = after.to_dict() if after else {}
    for field in fields:
        if before_map.get(field) != after_map.get(field):
            diff[field] = (before_map.get(field), after_map.get(field))
    return diff


@dataclass(frozen=True)
class SyncTarget:
    """Resolved list target for a library media item."""

    list_media_key: str
    entry: ListEntry
    mapping_descriptors: tuple[MappingDescriptor, ...] = ()
    mappings: tuple[AnibridgeDescriptorMapping, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedListTarget:
    """Resolved list key with mapping descriptors and ranges."""

    list_media_key: str
    mapping_descriptors: tuple[MappingDescriptor, ...]
    mappings: tuple[AnibridgeDescriptorMapping, ...]


@dataclass(slots=True)
class _GroupedTargets:
    descriptors: set[MappingDescriptor]
    mappings: dict[
        tuple[MappingDescriptor, MappingDescriptor],
        AnibridgeDescriptorMapping,
    ]


def find_best_search_result(
    title: str,
    results: Sequence[ListEntry],
    threshold: int,
) -> ListEntry | None:
    """Return the highest-scoring fuzzy match for a title.

    Args:
        title (str): Library title to match against list entries.
        results (Sequence[ListEntry]): Candidate list entries returned by search.
        threshold (int): Minimum score required to accept a match.

    Returns:
        ListEntry | None: The best matching entry, or None if no result meets
            the threshold.
    """
    best_entry: ListEntry | None = None
    best_ratio = 0
    for entry in results:
        candidates = {entry.title}
        media_title = entry.media().title
        if media_title:
            candidates.add(media_title)
        for candidate in candidates:
            if not candidate:
                continue
            ratio = fuzz.ratio(title, candidate)
            if ratio > best_ratio:
                best_ratio = ratio
                best_entry = entry
    if best_ratio < threshold:
        return None
    return best_entry


def _build_resolved_target(key: str, payload: _GroupedTargets) -> ResolvedListTarget:
    """Build a resolved target from grouped descriptors and ranges."""
    descriptors = tuple(sorted(payload.descriptors, key=descriptor_key))
    mappings = tuple(
        descriptor_mapping
        for _key, descriptor_mapping in sorted(
            payload.mappings.items(),
            key=lambda item: (
                descriptor_key(item[0][0]),
                descriptor_key(item[0][1]),
            ),
        )
        if descriptor_mapping.mappings
    )
    return ResolvedListTarget(
        list_media_key=key,
        mapping_descriptors=descriptors,
        mappings=mappings,
    )


def _order_target_for_descriptor_set(
    descriptor_set: tuple[MappingDescriptor, ...],
    target: ResolvedListTarget,
) -> ResolvedListTarget | None:
    """Order target metadata to match the original descriptor set."""
    ordered_descriptors = tuple(
        descriptor
        for descriptor in descriptor_set
        if descriptor in target.mapping_descriptors
    )
    ordered_mappings = tuple(
        mapping
        for descriptor in descriptor_set
        for mapping in target.mappings
        if mapping.source == descriptor
    )
    if not ordered_descriptors and not ordered_mappings:
        return None
    return ResolvedListTarget(
        list_media_key=target.list_media_key,
        mapping_descriptors=ordered_descriptors or target.mapping_descriptors,
        mappings=ordered_mappings,
    )


async def resolve_list_targets_batch(
    *,
    animap_client: AnimapClient,
    list_provider: ListProvider,
    descriptor_sets: Sequence[Sequence[MappingDescriptor]],
) -> list[tuple[ResolvedListTarget, ...]]:
    """Resolve mapping descriptors into list targets in a single batch.

    Args:
        animap_client (AnimapClient): Animap client used to resolve mapping edges.
        list_provider (ListProvider): List provider used to resolve target descriptors.
        descriptor_sets (Sequence[Sequence[MappingDescriptor]]): Mapping
            descriptors grouped by source item.

    Returns:
        list[tuple[ResolvedListTarget, ...]]: Resolved targets for each
            descriptor group.
    """
    normalized: list[tuple[MappingDescriptor, ...]] = []
    all_descriptors: set[MappingDescriptor] = set()
    for descriptor_set in descriptor_sets:
        ordered = tuple(dict.fromkeys(descriptor_set))
        normalized.append(ordered)
        all_descriptors.update(ordered)

    if not all_descriptors:
        return [tuple() for _ in normalized]

    grouped_edges = animap_client.resolve_edges_grouped(
        list(all_descriptors),
        target_providers=list_provider.MAPPING_PROVIDERS,
    )
    mappings_by_target: dict[
        MappingDescriptor,
        dict[MappingDescriptor, AnibridgeDescriptorMapping],
    ] = {}
    for target_descriptor, sources in grouped_edges.items():
        source_map: dict[MappingDescriptor, AnibridgeDescriptorMapping] = {}
        for source_descriptor, source_ranges in sources.items():
            descriptor_mapping = AnibridgeDescriptorMapping(
                source=source_descriptor,
                target=target_descriptor,
            )
            for source_range, destination_range in source_ranges:
                if destination_range is None:
                    continue
                descriptor_mapping.add_mapping(
                    source_range=source_range,
                    target_ranges=destination_range,
                )
            if descriptor_mapping.mappings:
                source_map[source_descriptor] = descriptor_mapping
        if source_map:
            mappings_by_target[target_descriptor] = source_map

    direct_targets = [
        descriptor
        for descriptor in all_descriptors
        if descriptor[0] in list_provider.MAPPING_PROVIDERS
    ]
    target_descriptors = {
        *mappings_by_target.keys(),
        *direct_targets,
    }
    if not target_descriptors:
        return [tuple() for _ in normalized]

    resolved_targets = await list_provider.resolve_mapping_descriptors(
        list(target_descriptors)
    )

    grouped: dict[str, _GroupedTargets] = {}
    for target in resolved_targets:
        group = grouped.setdefault(
            target.media_key,
            _GroupedTargets(descriptors=set(), mappings={}),
        )
        group.descriptors.add(target.descriptor)
        if target.descriptor not in mappings_by_target:
            continue
        for source_descriptor, descriptor_mapping in mappings_by_target[
            target.descriptor
        ].items():
            key = (source_descriptor, target.descriptor)
            merged = group.mappings.setdefault(
                key,
                AnibridgeDescriptorMapping(
                    source=source_descriptor,
                    target=target.descriptor,
                ),
            )
            merged.mappings.extend(descriptor_mapping.mappings)

    targets_by_key = {
        key: _build_resolved_target(key, grouped[key]) for key in sorted(grouped.keys())
    }

    results: list[tuple[ResolvedListTarget, ...]] = []
    for descriptor_set in normalized:
        filtered = tuple(
            ordered_target
            for target in targets_by_key.values()
            if (
                ordered_target := _order_target_for_descriptor_set(
                    descriptor_set, target
                )
            )
            is not None
        )
        results.append(filtered)
    return results


async def resolve_list_targets(
    *,
    animap_client: AnimapClient,
    list_provider: ListProvider,
    media_items: Sequence[LibraryEntry],
) -> tuple[ResolvedListTarget, ...]:
    """Resolve mapping descriptors for one logical media item.

    Args:
        animap_client (AnimapClient): Animap client used to resolve mapping edges.
        list_provider (ListProvider): List provider used to resolve target descriptors.
        media_items (Sequence[LibraryEntry]): Library items whose descriptors
            should be combined.

    Returns:
        tuple[ResolvedListTarget, ...]: Resolved targets for the supplied media items.
    """
    descriptors: list[MappingDescriptor] = []
    for media in media_items:
        descriptors.extend(media.mapping_descriptors())
    resolved = await resolve_list_targets_batch(
        animap_client=animap_client,
        list_provider=list_provider,
        descriptor_sets=[descriptors],
    )
    return resolved[0] if resolved else tuple()
