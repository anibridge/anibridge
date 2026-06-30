"""Service for managing normalized record-field pins per profile."""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Annotated, ClassVar

import msgspec
from anibridge.provider.base import (
    Artwork,
    FacetName,
    Node,
    NodeQuery,
    RecordField,
    Ref,
    SupportsNodeReads,
    SupportsNodeSearch,
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
    "PinFieldOption",
    "PinSearchResult",
    "PinService",
    "get_pin_service",
]


_PIN_LABELS: dict[str, str] = {
    RecordField.STATUS.value: "Status",
    RecordField.RATING.value: "Rating",
    RecordField.PROGRESS.value: "Progress",
    RecordField.NOTES.value: "Notes",
    RecordField.REPEAT_COUNT.value: "Repeat Count",
    RecordField.STARTED_AT.value: "Started Date",
    RecordField.FINISHED_AT.value: "Finished Date",
    RecordField.LAST_ACTIVITY_AT.value: "Last Activity",
}

_FIELD_VALUES: tuple[str, ...] = tuple(field.value for field in RecordField)
_FIELD_SET: frozenset[str] = frozenset(_FIELD_VALUES)


class PinFieldOption(msgspec.Struct):
    """Metadata for a selectable pin field."""

    value: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Record field identifier that can be pinned.",
            examples=["status"],
        ),
    ]
    label: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Human-friendly label for the pin field option.",
            examples=["Status"],
        ),
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
    target_ref: Annotated[
        RefPayload,
        msgspec.Meta(
            description="Normalized target provider ref for the pinned entry.",
            examples=[{"key": "5114", "path": []}],
        ),
    ]
    fields: Annotated[
        list[str],
        msgspec.Meta(
            min_length=1,
            description="Ordered set of normalized record fields pinned for the entry.",
            examples=[["status", "progress"]],
        ),
    ] = msgspec.field(default_factory=list)
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

    allowed_fields: ClassVar[tuple[str, ...]] = _FIELD_VALUES
    allowed_field_set: ClassVar[frozenset[str]] = _FIELD_SET

    def list_options(self) -> list[PinFieldOption]:
        """Return metadata for selectable fields."""
        return [
            PinFieldOption(value=value, label=_PIN_LABELS.get(value, value.title()))
            for value in self.allowed_fields
        ]

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
        """Return a single anchor-ref pin entry if it exists."""
        target_namespace = self._target_namespace(profile)
        target_ref = ref_to_json(Ref.anchor(media_key))
        with db() as ctx:
            pin = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == profile,
                    Pin.target_namespace == target_namespace,
                    Pin.target_ref == target_ref,
                )
                .first()
            )

        return self._serialize(pin) if pin else None

    def _upsert_pin(
        self,
        profile: str,
        media_key: str,
        fields: Iterable[str],
    ) -> PinEntry:
        """Create or update a pin configuration."""
        sanitized = self._sanitize_fields(fields)
        if not sanitized:
            raise ValueError("At least one field must be provided")
        target_namespace = self._target_namespace(profile)
        target_ref = ref_to_json(Ref.anchor(media_key))

        with db() as ctx:
            pin = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == profile,
                    Pin.target_namespace == target_namespace,
                    Pin.target_ref == target_ref,
                )
                .first()
            )

            now = datetime.now(UTC)
            if not pin:
                pin = Pin(
                    profile_name=profile,
                    target_namespace=target_namespace,
                    target_ref=target_ref,
                    fields=sanitized,
                    created_at=now,
                    updated_at=now,
                )
                ctx.session.add(pin)
            else:
                pin.fields = sanitized
                pin.updated_at = now

            ctx.session.commit()
            ctx.session.refresh(pin)

        return self._serialize(pin)

    async def _fetch_target_metadata(
        self,
        profile: str,
        refs: Sequence[RefPayload],
    ) -> dict[RefKey, ProviderMediaMetadata]:
        """Fetch target node metadata for pinned refs when supported."""
        if not refs:
            return {}
        bridge = get_bridge(profile)
        provider_refs: list[Ref] = []
        for item in refs:
            ref = ref_from_payload(item)
            if ref is not None:
                provider_refs.append(ref)
        if not isinstance(bridge.target_provider, SupportsNodeReads):
            return {}

        page = await bridge.target_provider.fetch_nodes(
            NodeQuery(
                refs=tuple(provider_refs),
                facets=frozenset({FacetName.ARTWORK}),
            )
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
        """Return existing pins keyed by provider ref."""
        if not refs:
            return {}
        ref_json = [ref_to_json(ref) for ref in refs]
        with db() as ctx:
            rows = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == profile,
                    Pin.target_namespace == target_namespace,
                    Pin.target_ref.in_(ref_json),
                )
                .all()
            )
        pins: dict[RefKey, PinEntry] = {}
        for row in rows:
            entry = self._serialize(row)
            pins[RefKey.from_payload(entry.target_ref)] = entry
        return pins

    async def list_pins(self, profile: str, with_media: bool = False) -> list[PinEntry]:
        """Return all pins for a profile ordered by most recent."""
        pins = self._list_pins(profile)
        if with_media and pins:
            metadata = await self._fetch_target_metadata(
                profile,
                [pin.target_ref for pin in pins],
            )
            return [
                PinEntry(
                    profile_name=pin.profile_name,
                    target_namespace=pin.target_namespace,
                    target_ref=pin.target_ref,
                    fields=list(pin.fields),
                    created_at=pin.created_at,
                    updated_at=pin.updated_at,
                    media=metadata.get(RefKey.from_payload(pin.target_ref)),
                )
                for pin in pins
            ]
        return pins

    async def get_pin(
        self,
        profile: str,
        media_key: str,
        with_media: bool = False,
    ) -> PinEntry | None:
        """Return a single pin entry if it exists."""
        entry = self._get_pin(profile, media_key)
        if not entry:
            return None
        if with_media:
            metadata = await self._fetch_target_metadata(profile, [entry.target_ref])
            return PinEntry(
                profile_name=entry.profile_name,
                target_namespace=entry.target_namespace,
                target_ref=entry.target_ref,
                fields=list(entry.fields),
                created_at=entry.created_at,
                updated_at=entry.updated_at,
                media=metadata.get(RefKey(key=media_key)),
            )
        return entry

    async def upsert_pin(
        self,
        profile: str,
        media_key: str,
        fields: Iterable[str],
        with_media: bool = False,
    ) -> PinEntry:
        """Create or update a pin configuration."""
        entry = self._upsert_pin(profile, media_key, fields)
        if with_media:
            metadata = await self._fetch_target_metadata(profile, [entry.target_ref])
            return PinEntry(
                profile_name=entry.profile_name,
                target_namespace=entry.target_namespace,
                target_ref=entry.target_ref,
                fields=list(entry.fields),
                created_at=entry.created_at,
                updated_at=entry.updated_at,
                media=metadata.get(RefKey(key=media_key)),
            )
        return entry

    def delete_pin(self, profile: str, media_key: str) -> None:
        """Remove a pin configuration if it exists."""
        target_namespace = self._target_namespace(profile)
        target_ref = ref_to_json(Ref.anchor(media_key))
        with db() as ctx:
            pin = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == profile,
                    Pin.target_namespace == target_namespace,
                    Pin.target_ref == target_ref,
                )
                .first()
            )
            if not pin:
                return
            ctx.session.delete(pin)
            ctx.session.commit()

    def _sanitize_fields(self, fields: Iterable[str]) -> list[str]:
        """Normalize and validate requested pin fields."""
        requested: list[str] = []
        for field in fields:
            value = str(field).strip().lower()
            if not value:
                continue
            if value not in self.allowed_field_set:
                raise ValueError(f"Unsupported field '{field}'")
            if value not in requested:
                requested.append(value)

        return [field for field in self.allowed_fields if field in requested]

    @staticmethod
    def _serialize(pin: Pin, media: ProviderMediaMetadata | None = None) -> PinEntry:
        """Serialize a database pin row."""
        target_ref = ref_payload_from_json(pin.target_ref)
        if target_ref is None:
            raise ValueError("Pin row is missing a target ref")
        return PinEntry(
            profile_name=pin.profile_name,
            target_namespace=pin.target_namespace,
            target_ref=target_ref,
            fields=list(pin.fields or []),
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
