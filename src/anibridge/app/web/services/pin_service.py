"""Service for managing target parent pins per profile."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, ClassVar, cast

import msgspec
from anibridge.provider.base import (
    Artwork,
    FacetName,
    Node,
    NodeQuery,
    Page,
    Ref,
    SupportsNodeSearch,
    SupportsReads,
)
from anibridge.utils.cache import cache

from anibridge.app.config.database import db
from anibridge.app.config.settings import get_config
from anibridge.app.core.sync import (
    RefKey,
    RefPayload,
    ref_from_payload,
    ref_payload_from_json,
    ref_to_json,
    ref_to_key,
)
from anibridge.app.models.db.pin import Pin
from anibridge.app.models.schemas.provider import ProviderMediaMetadata
from anibridge.app.web.state import get_bridge

__all__ = [
    "PinEntry",
    "PinSearchResult",
    "PinService",
    "get_pin_service",
]


class PinEntry(msgspec.Struct):
    """Serialized representation of a pin row."""

    profile_name: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Profile that owns the pinned entry.",
            examples=["default"],
        ),
    ]
    target_namespace: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Target provider namespace for the pinned entry.",
            examples=["anilist"],
        ),
    ]
    target_parent_ref: Annotated[
        RefPayload,
        msgspec.Meta(
            description="Normalized target parent ref for the pinned entry.",
            examples=[{"key": "5114", "path": []}],
        ),
    ]
    created_at: Annotated[
        datetime,
        msgspec.Meta(
            description="UTC timestamp when the pin was first created.",
            examples=["2026-01-01T00:00:00Z"],
        ),
    ] = msgspec.field(default_factory=lambda: datetime.now(UTC))
    updated_at: Annotated[
        datetime,
        msgspec.Meta(
            description="UTC timestamp when the pin was last updated.",
            examples=["2026-01-01T00:05:00Z"],
        ),
    ] = msgspec.field(default_factory=lambda: datetime.now(UTC))
    media: (
        Annotated[
            ProviderMediaMetadata,
            msgspec.Meta(
                description="Resolved provider metadata for the pinned target ref.",
                examples=[{"namespace": "anilist", "key": "5114"}],
            ),
        ]
        | None
    ) = None


class PinSearchResult(msgspec.Struct):
    """Serialized provider search result with current pin state."""

    media: Annotated[
        ProviderMediaMetadata,
        msgspec.Meta(
            description="Provider metadata for the matched target media.",
            examples=[{"namespace": "anilist", "key": "5114"}],
        ),
    ]
    pin: (
        Annotated[
            PinEntry,
            msgspec.Meta(
                description="Existing pin state for the matched item when present.",
            ),
        ]
        | None
    ) = None


class PinService:
    """Service encapsulating pin CRUD operations."""

    parent_ref_path: ClassVar[tuple[object, ...]] = ()

    @staticmethod
    def _target_namespace(profile: str) -> str:
        """Return the configured target provider namespace for a profile."""
        return get_config().get_profile(profile).target_provider

    def _list_pins(self, profile: str) -> list[PinEntry]:
        """Return all pins for a profile ordered by most recent."""
        target_namespace = self._target_namespace(profile)
        with db() as ctx:
            rows = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == profile,
                    Pin.target_namespace == target_namespace,
                )
                .order_by(Pin.updated_at.desc())
                .all()
            )

        return [self._serialize(row) for row in rows]

    def _get_pin(self, profile: str, media_key: str) -> PinEntry | None:
        """Return a single target parent pin entry if it exists."""
        return self._get_pin_by_ref(profile, RefPayload(media_key))

    def _get_pin_by_ref(self, profile: str, ref: RefPayload) -> PinEntry | None:
        """Return a single target parent pin entry by exact ref."""
        target_namespace = self._target_namespace(profile)
        target_parent_ref = RefKey.from_payload(ref).to_json()
        with db() as ctx:
            pin = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == profile,
                    Pin.target_namespace == target_namespace,
                    Pin.target_parent_ref == target_parent_ref,
                )
                .first()
            )

        return self._serialize(pin) if pin else None

    def _upsert_pin(
        self,
        profile: str,
        media_key: str,
    ) -> PinEntry:
        """Create or update a target parent pin."""
        return self._upsert_pin_by_ref(profile, RefPayload(media_key))

    def _upsert_pin_by_ref(self, profile: str, ref: RefPayload) -> PinEntry:
        """Create or update a target parent pin by exact ref."""
        target_namespace = self._target_namespace(profile)
        target_parent_ref = RefKey.from_payload(ref).to_json()

        with db() as ctx:
            pin = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == profile,
                    Pin.target_namespace == target_namespace,
                    Pin.target_parent_ref == target_parent_ref,
                )
                .first()
            )

            now = datetime.now(UTC)
            if not pin:
                pin = Pin(
                    profile_name=profile,
                    target_namespace=target_namespace,
                    target_parent_ref=target_parent_ref,
                    created_at=now,
                    updated_at=now,
                )
                ctx.session.add(pin)
            else:
                pin.updated_at = now

            ctx.session.commit()
            ctx.session.refresh(pin)

        return self._serialize(pin)

    async def _fetch_target_metadata(
        self,
        profile: str,
        refs: Sequence[RefPayload],
    ) -> dict[RefKey, ProviderMediaMetadata]:
        """Fetch target node metadata for pinned parent refs when supported."""
        if not refs:
            return {}
        bridge = get_bridge(profile)
        provider_refs: list[Ref] = []
        for item in refs:
            ref = ref_from_payload(item)
            if ref is not None:
                provider_refs.append(ref)
        if not isinstance(bridge.target_provider, SupportsReads):
            return {}

        page = cast(
            Page[Node],
            await bridge.target_provider.fetch(
                NodeQuery(
                    refs=tuple(provider_refs),
                    facets=frozenset({FacetName.ARTWORK}),
                )
            ),
        )
        metadata: dict[RefKey, ProviderMediaMetadata] = {}
        for node in page.items:
            metadata[ref_to_key(node.ref)] = self._metadata_from_node(
                bridge.target_provider.NAMESPACE,
                node,
            )
        return metadata

    async def search_pins(
        self,
        profile: str,
        query: str,
        *,
        limit: int = 10,
    ) -> list[PinSearchResult]:
        """Search target provider media and attach existing pin state."""
        text = query.strip()
        if not text:
            return []

        bridge = get_bridge(profile)
        target = bridge.target_provider
        if not isinstance(target, SupportsNodeSearch):
            raise ValueError(
                f"Target provider '{target.NAMESPACE}' does not support media search"
            )

        bounded_limit = min(max(limit, 1), 50)
        page = await target.search_nodes(
            text,
            limit=bounded_limit,
            facets=frozenset({FacetName.ARTWORK}),
        )

        refs = [node.ref for node in page.items if node.ref.is_anchor]
        pins_by_key = self._get_pins_for_refs(profile, target.NAMESPACE, refs)
        return [
            PinSearchResult(
                media=self._metadata_from_node(target.NAMESPACE, node),
                pin=pins_by_key.get(ref_to_key(node.ref)),
            )
            for node in page.items
            if node.ref.is_anchor
        ]

    def _get_pins_for_refs(
        self,
        profile: str,
        target_namespace: str,
        refs: Sequence[Ref],
    ) -> dict[RefKey, PinEntry]:
        """Return existing pins keyed by target parent ref."""
        if not refs:
            return {}
        ref_json = [ref_to_json(ref) for ref in refs]
        with db() as ctx:
            rows = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == profile,
                    Pin.target_namespace == target_namespace,
                    Pin.target_parent_ref.in_(ref_json),
                )
                .all()
            )
        pins: dict[RefKey, PinEntry] = {}
        for row in rows:
            entry = self._serialize(row)
            pins[RefKey.from_payload(entry.target_parent_ref)] = entry
        return pins

    async def list_pins(self, profile: str, with_media: bool = False) -> list[PinEntry]:
        """Return all pins for a profile ordered by most recent."""
        pins = self._list_pins(profile)
        if with_media and pins:
            metadata = await self._fetch_target_metadata(
                profile,
                [pin.target_parent_ref for pin in pins],
            )
            return [
                PinEntry(
                    profile_name=pin.profile_name,
                    target_namespace=pin.target_namespace,
                    target_parent_ref=pin.target_parent_ref,
                    created_at=pin.created_at,
                    updated_at=pin.updated_at,
                    media=metadata.get(RefKey.from_payload(pin.target_parent_ref)),
                )
                for pin in pins
            ]
        return pins

    async def get_pin(
        self,
        profile: str,
        media_key: str,
        with_media: bool = False,
        target_ref: RefPayload | None = None,
    ) -> PinEntry | None:
        """Return a single pin entry if it exists."""
        entry = (
            self._get_pin_by_ref(profile, target_ref)
            if target_ref is not None
            else self._get_pin(profile, media_key)
        )
        if not entry:
            return None
        if with_media:
            metadata = await self._fetch_target_metadata(
                profile, [entry.target_parent_ref]
            )
            return PinEntry(
                profile_name=entry.profile_name,
                target_namespace=entry.target_namespace,
                target_parent_ref=entry.target_parent_ref,
                created_at=entry.created_at,
                updated_at=entry.updated_at,
                media=metadata.get(RefKey.from_payload(entry.target_parent_ref)),
            )
        return entry

    async def upsert_pin(
        self,
        profile: str,
        media_key: str,
        with_media: bool = False,
        target_ref: RefPayload | None = None,
    ) -> PinEntry:
        """Create or update a target parent pin."""
        entry = (
            self._upsert_pin_by_ref(profile, target_ref)
            if target_ref is not None
            else self._upsert_pin(profile, media_key)
        )
        if with_media:
            metadata = await self._fetch_target_metadata(
                profile, [entry.target_parent_ref]
            )
            return PinEntry(
                profile_name=entry.profile_name,
                target_namespace=entry.target_namespace,
                target_parent_ref=entry.target_parent_ref,
                created_at=entry.created_at,
                updated_at=entry.updated_at,
                media=metadata.get(RefKey.from_payload(entry.target_parent_ref)),
            )
        return entry

    def delete_pin(
        self,
        profile: str,
        media_key: str,
        target_ref: RefPayload | None = None,
    ) -> None:
        """Remove a target parent pin if it exists."""
        target_namespace = self._target_namespace(profile)
        target_parent_ref = RefKey.from_payload(
            target_ref if target_ref is not None else RefPayload(media_key)
        ).to_json()
        with db() as ctx:
            pin = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == profile,
                    Pin.target_namespace == target_namespace,
                    Pin.target_parent_ref == target_parent_ref,
                )
                .first()
            )
            if not pin:
                return
            ctx.session.delete(pin)
            ctx.session.commit()

    @staticmethod
    def _serialize(pin: Pin, media: ProviderMediaMetadata | None = None) -> PinEntry:
        """Serialize a database pin row."""
        target_parent_ref = ref_payload_from_json(pin.target_parent_ref)
        if target_parent_ref is None:
            raise ValueError("Pin row is missing a target parent ref")
        return PinEntry(
            profile_name=pin.profile_name,
            target_namespace=pin.target_namespace,
            target_parent_ref=target_parent_ref,
            created_at=pin.created_at,
            updated_at=pin.updated_at,
            media=media,
        )

    @staticmethod
    def _metadata_from_node(namespace: str, node: Node) -> ProviderMediaMetadata:
        """Serialize provider node metadata for web responses."""
        artwork = node.facets.get(FacetName.ARTWORK)
        return ProviderMediaMetadata(
            namespace=namespace,
            key=node.ref.key,
            title=node.title,
            poster_url=artwork.poster if isinstance(artwork, Artwork) else None,
            external_url=node.url,
            labels=list(node.labels) if node.labels else None,
        )


@cache
def get_pin_service() -> PinService:
    """Return cached pin service instance."""
    return PinService()
