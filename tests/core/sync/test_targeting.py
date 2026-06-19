"""Unit tests for `anibridge.app.core.sync.targeting`."""

from collections.abc import Sequence
from logging import getLogger
from typing import cast

import pytest
from anibridge.provider.base import (
    Account,
    Capabilities,
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

from anibridge.app.core.animap import AnimapClient, AnimapEdge
from anibridge.app.core.sync.targeting import TargetResolver


class _FakeAnimapClient:
    """Mapping client double with no cross-provider edges."""

    def resolve_edges(self, descriptors, *, target_providers=None):
        return ()


class _EdgeAnimapClient:
    """Mapping client double that returns configured edges."""

    def __init__(self, *edges: AnimapEdge) -> None:
        self.edges = edges
        self.calls: list[tuple[object, object]] = []

    def resolve_edges(self, descriptors, *, target_providers=None):
        self.calls.append((descriptors, target_providers))
        return self.edges


class _PlainProvider(Provider):
    """Provider without mapping support."""

    DISPLAY_NAME = "Plain"
    NAMESPACE = "plain"

    def account(self) -> Account | None:
        return None


class _MappingTargetProvider(Provider, SupportsMapping):
    """Target provider whose provider namespace differs from its mapping authority."""

    DISPLAY_NAME = "Target"
    NAMESPACE = "target-provider"

    def __init__(self, *, authorities: frozenset[str]) -> None:
        super().__init__(logger=getLogger(__name__), config={})
        self.authorities = authorities
        self.resolved_ids: list[ExternalId] = []

    def account(self) -> Account | None:
        return None

    def capabilities(self) -> Capabilities:
        return Capabilities(external_authorities=self.authorities)

    async def resolve(self, ids: Sequence[ExternalId]) -> Sequence[Match]:
        self.resolved_ids.extend(ids)
        return tuple(
            Match(
                external_id=item,
                ref=Ref.anchor(f"resolved:{item.authority}:{item.value}"),
                confidence=1.0,
            )
            for item in ids
            if item.authority in self.authorities
        )


class _DuplicateMatchProvider(_MappingTargetProvider):
    """Mapping provider that returns duplicate refs with different confidence."""

    async def resolve(self, ids: Sequence[ExternalId]) -> Sequence[Match]:
        self.resolved_ids.extend(ids)
        return tuple(
            Match(
                external_id=item,
                ref=Ref.anchor("same-target"),
                confidence=0.9 if item.value == "456" else 0.5,
            )
            for item in ids
        )


def test_external_ids_for_record_deduplicates_source_record_and_node_ids() -> None:
    """Record ids and node IDS facet are merged by mapping descriptor."""
    anilist = ExternalId("anilist", "1")
    node = Node(
        ref=Ref.anchor("source-1"),
        kind="anime",
        facets={
            FacetName.IDS: Identifiers(
                ids=(anilist, ExternalId("tmdb_show", "10")),
            )
        },
    )
    record = Record(
        ref=Ref.anchor("source-1"),
        kind="progress",
        ids=(anilist, ExternalId("tvdb_show", "20")),
    )

    ids = TargetResolver._record_ids(node=node, record=record)

    assert ids == (
        ExternalId("anilist", "1"),
        ExternalId("tvdb_show", "20"),
        ExternalId("tmdb_show", "10"),
    )


@pytest.mark.asyncio
async def test_resolve_target_refs_uses_mapping_authority() -> None:
    """Namespace equality must not bypass the target's advertised authorities."""
    provider = _MappingTargetProvider(authorities=frozenset({"anilist"}))
    node = Node(ref=Ref.anchor("source-native"), kind="anime")
    record = Record(
        ref=Ref.anchor("source-native"),
        kind="progress",
        ids=(ExternalId("anilist", "123"),),
    )

    resolver = TargetResolver(
        target_provider=provider,
        animap_client=cast(AnimapClient, _FakeAnimapClient()),
    )
    targets = await resolver.resolve(node=node, record=record)

    assert tuple(match.match.ref for match in targets) == (
        Ref.anchor("resolved:anilist:123"),
    )
    assert provider.resolved_ids == [ExternalId("anilist", "123")]


@pytest.mark.asyncio
async def test_resolve_target_refs_skips_namespace_match_without_authority() -> None:
    """Same provider namespace is not enough when no target authority matches."""
    provider = _MappingTargetProvider(authorities=frozenset({"anilist"}))
    node = Node(ref=Ref.anchor("source-native"), kind="anime")
    record = Record(ref=Ref.anchor("source-native"), kind="progress")

    resolver = TargetResolver(
        target_provider=provider,
        animap_client=cast(AnimapClient, _FakeAnimapClient()),
    )
    targets = await resolver.resolve(node=node, record=record)

    assert targets == ()
    assert provider.resolved_ids == []


@pytest.mark.asyncio
async def test_resolve_target_refs_requires_advertised_authorities() -> None:
    """Empty target authorities should not query AniMap without a provider filter."""
    provider = _MappingTargetProvider(authorities=frozenset())
    node = Node(ref=Ref.anchor("source-native"), kind="anime")
    record = Record(
        ref=Ref.anchor("source-native"),
        kind="progress",
        ids=(ExternalId("anilist", "123"),),
    )

    resolver = TargetResolver(
        target_provider=provider,
        animap_client=cast(AnimapClient, _FakeAnimapClient()),
    )
    targets = await resolver.resolve(node=node, record=record)

    assert targets == ()
    assert provider.resolved_ids == []


@pytest.mark.asyncio
async def test_resolve_target_refs_requires_mapping_capability() -> None:
    """Providers without mapping support cannot resolve source ids."""
    node = Node(ref=Ref.anchor("source-native"), kind="anime")
    record = Record(
        ref=Ref.anchor("source-native"),
        kind="progress",
        ids=(ExternalId("anilist", "123"),),
    )

    resolver = TargetResolver(
        target_provider=_PlainProvider(logger=getLogger(__name__), config={}),
        animap_client=cast(AnimapClient, _FakeAnimapClient()),
    )
    targets = await resolver.resolve(node=node, record=record)

    assert targets == ()


@pytest.mark.asyncio
async def test_resolve_target_refs_uses_animap_edges_and_prefers_confidence() -> None:
    """AniMap edges should add target ids and collapse duplicate refs by confidence."""
    provider = _DuplicateMatchProvider(authorities=frozenset({"anilist"}))
    node = Node(ref=Ref.anchor("source-native"), kind="anime")
    record = Record(
        ref=Ref.anchor("source-native"),
        kind="progress",
        ids=(ExternalId("tmdb_show", "10"), ExternalId("anilist", "123")),
    )
    valid_edge = AnimapEdge(
        source=("tmdb_show", "10", "s1"),
        destination=("anilist", "456", None),
        source_range="1-12",
        destination_range="1",
    )
    null_destination_edge = AnimapEdge(
        source=("tmdb_show", "10", None),
        destination=("anilist", "ignored", None),
        source_range="1",
        destination_range=None,
    )
    animap_client = _EdgeAnimapClient(valid_edge, null_destination_edge)

    resolver = TargetResolver(
        target_provider=provider,
        animap_client=cast(AnimapClient, animap_client),
    )
    targets = await resolver.resolve(node=node, record=record)

    assert len(targets) == 1
    assert targets[0].match.external_id == ExternalId("anilist", "456")
    assert targets[0].source_id == ExternalId("tmdb_show", "10", "s1")
    assert targets[0].target_id == ExternalId("anilist", "456")
    assert targets[0].mappings[0].source_key == "1-12"
    assert targets[0].mappings
    assert provider.resolved_ids == [
        ExternalId("anilist", "123"),
        ExternalId("anilist", "456"),
    ]
    assert animap_client.calls[0][1] == frozenset({"anilist"})


@pytest.mark.asyncio
async def test_resolve_target_refs_deduplicates_identical_mapping_edges() -> None:
    """Duplicate AniMap rows should not duplicate mapping ranges in sync plans."""
    provider = _MappingTargetProvider(authorities=frozenset({"anilist"}))
    node = Node(ref=Ref.anchor("source-native"), kind="anime")
    record = Record(
        ref=Ref.anchor("source-native"),
        kind="progress",
        ids=(ExternalId("tvdb_show", "281949", "s2"),),
    )
    edge = AnimapEdge(
        source=("tvdb_show", "281949", "s2"),
        destination=("anilist", "21006", None),
        source_range="1-10",
        destination_range="1-10",
    )

    resolver = TargetResolver(
        target_provider=provider,
        animap_client=cast(AnimapClient, _EdgeAnimapClient(edge, edge)),
    )
    targets = await resolver.resolve(node=node, record=record)

    assert len(targets) == 1
    assert [mapping.as_pair() for mapping in targets[0].mappings] == [("1-10", "1-10")]
