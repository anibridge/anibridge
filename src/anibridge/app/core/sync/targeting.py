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


class _TargetCandidate(msgspec.Struct, frozen=True):
    external_id: ExternalId
    mappings: tuple[AnibridgeMapping, ...] = ()
    source_id: ExternalId | None = None

    def with_mapping(
        self,
        *,
        mapping: AnibridgeMapping,
        source_id: ExternalId,
    ) -> _TargetCandidate:
        """Return a new candidate with the given mapping added."""
        if mapping in self.mappings:
            return self
        return _TargetCandidate(
            external_id=self.external_id,
            mappings=(*self.mappings, mapping),
            source_id=self.source_id or source_id,
        )


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

        candidates = self._candidate_ids(node=node, record=record)
        if not candidates:
            return ()

        matches = await self.target_provider.resolve(
            tuple(candidate.external_id for candidate in candidates.values())
        )
        by_ref: dict[Ref, ResolvedTarget] = {}
        for match in matches:
            candidate = candidates.get(match.external_id.descriptor)
            if candidate is None:
                continue

            resolved = ResolvedTarget(
                match=match,
                mappings=candidate.mappings,
                source_id=candidate.source_id,
                target_id=candidate.external_id,
            )
            existing = by_ref.get(match.ref)
            if existing is None or self._is_better_match(resolved, existing):
                by_ref[match.ref] = resolved

        return tuple(by_ref.values())

    @staticmethod
    def _is_better_match(
        candidate: ResolvedTarget,
        existing: ResolvedTarget,
    ) -> bool:
        """Return whether a candidate should replace an existing ref match."""
        candidate_confidence = candidate.match.confidence or 0
        existing_confidence = existing.match.confidence or 0
        if candidate_confidence != existing_confidence:
            return candidate_confidence > existing_confidence
        if bool(candidate.mappings) != bool(existing.mappings):
            return bool(candidate.mappings)
        return False

    def _candidate_ids(
        self,
        *,
        node: Node,
        record: Record,
    ) -> dict[str, _TargetCandidate]:
        """Return all potential target candidates for a given record."""
        ids = self._record_ids(node=node, record=record)
        candidates = {
            item.descriptor: _TargetCandidate(item)
            for item in ids
            if item.authority in self.capabilities.external_authorities
        }

        descriptors = tuple((item.authority, item.value, item.scope) for item in ids)
        edges = self.animap_client.resolve_edges(
            descriptors,
            target_providers=self.capabilities.external_authorities,
        )
        for edge in edges:
            if edge.destination_range is None:
                continue

            mapping = AnibridgeMapping.parse(edge.source_range, edge.destination_range)
            if mapping.target_weight == 0:
                continue

            target_id = ExternalId(
                authority=edge.destination[0],
                value=edge.destination[1],
                scope=edge.destination[2],
            )
            source_id = ExternalId(
                authority=edge.source[0],
                value=edge.source[1],
                scope=edge.source[2],
            )
            candidate = candidates.get(
                target_id.descriptor,
                _TargetCandidate(target_id, source_id=source_id),
            )
            candidates[target_id.descriptor] = candidate.with_mapping(
                mapping=mapping,
                source_id=source_id,
            )

        return candidates

    @staticmethod
    def _record_ids(*, node: Node, record: Record) -> tuple[ExternalId, ...]:
        """Return all external IDs associated with a record, including anchor facets."""
        ids: list[ExternalId] = list(record.ids)
        facet = node.facets.get(FacetName.IDS)
        if record.ref.is_anchor and isinstance(facet, Identifiers):
            ids.extend(facet.ids)

        deduped: dict[str, ExternalId] = {}
        for external_id in ids:
            deduped.setdefault(external_id.descriptor, external_id)
        return tuple(deduped.values())
