"""Target resolution for provider-backed sync."""

import msgspec
from anibridge.provider.base import (
    ExternalId,
    FacetName,
    Identifiers,
    Match,
    Node,
    Provider,
    Record,
    Ref,
    SupportsMapping,
)
from anibridge.utils.mappings import AnibridgeMapping

from anibridge.app.core.animap import AnimapClient

__all__ = ["ResolvedTarget", "TargetResolver"]


class ResolvedTarget(msgspec.Struct, frozen=True):
    """A provider match plus the mapping that led to it."""

    match: Match
    mappings: tuple[AnibridgeMapping, ...] = ()
    source_id: ExternalId | None = None
    target_id: ExternalId | None = None

    @property
    def ref(self) -> Ref:
        """Return the resolved target ref."""
        return self.match.ref


class TargetResolver:
    """Resolve source records to target provider refs."""

    def __init__(self, *, target_provider: Provider, animap_client: AnimapClient):
        """Initialize the resolver for one target provider."""
        self.target_provider = target_provider
        self.animap_client = animap_client
        self.capabilities = target_provider.capabilities()

    async def resolve(
        self,
        *,
        node: Node,
        record: Record,
    ) -> tuple[ResolvedTarget, ...]:
        """Resolve one source record to target matches."""
        if not isinstance(self.target_provider, SupportsMapping):
            return ()
        if not self.capabilities.external_authorities:
            return ()

        ids: dict[str, ExternalId] = {}
        for external_id in record.ids:
            ids.setdefault(external_id.descriptor, external_id)
        facet = node.facets.get(FacetName.IDS)
        if record.ref.is_anchor and isinstance(facet, Identifiers):
            for external_id in facet.ids:
                ids.setdefault(external_id.descriptor, external_id)

        candidates = {
            item.descriptor: item
            for item in ids.values()
            if item.authority in self.capabilities.external_authorities
        }
        candidate_mappings: dict[str, list[AnibridgeMapping]] = {}
        candidate_sources: dict[str, ExternalId] = {}

        descriptors = tuple(
            (item.authority, item.value, item.scope) for item in ids.values()
        )
        for edge in self.animap_client.resolve_edges(
            descriptors,
            target_providers=self.capabilities.external_authorities,
        ):
            if edge.destination_range is None:
                continue

            mapping = AnibridgeMapping.parse(edge.source_range, edge.destination_range)
            if mapping.target_weight == 0:
                continue

            target_id = ExternalId(*edge.destination)
            source_id = ExternalId(*edge.source)
            candidates.setdefault(target_id.descriptor, target_id)
            candidate_sources.setdefault(target_id.descriptor, source_id)
            mappings = candidate_mappings.setdefault(target_id.descriptor, [])
            if mapping not in mappings:
                mappings.append(mapping)

        if not candidates:
            return ()

        matches = await self.target_provider.resolve(tuple(candidates.values()))
        by_ref: dict[Ref, ResolvedTarget] = {}
        for match in matches:
            candidate = candidates.get(match.external_id.descriptor)
            if candidate is None:
                continue

            resolved = ResolvedTarget(
                match=match,
                mappings=tuple(candidate_mappings.get(candidate.descriptor, ())),
                source_id=candidate_sources.get(candidate.descriptor),
                target_id=candidate,
            )
            existing = by_ref.get(match.ref)
            if existing is None:
                by_ref[match.ref] = resolved
                continue

            confidence = resolved.match.confidence or 0
            existing_confidence = existing.match.confidence or 0
            if confidence > existing_confidence or (
                confidence == existing_confidence
                and bool(resolved.mappings)
                and not existing.mappings
            ):
                by_ref[match.ref] = resolved

        return tuple(by_ref.values())
