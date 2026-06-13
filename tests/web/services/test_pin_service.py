"""Unit tests for the pin management service."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from anibridge.provider.base import (
    Artwork,
    FacetName,
    Node,
    NodeQuery,
    Page,
    RecordField,
    Ref,
    SupportsNodeReads,
    SupportsNodeSearch,
)

from anibridge.app.config.database import db
from anibridge.app.core.sched.client import SchedulerClient
from anibridge.app.models.db.pin import Pin
from anibridge.app.web.services.pin_service import PinService
from anibridge.app.web.state import get_app_state


class DummyTargetProvider(SupportsNodeReads, SupportsNodeSearch):
    """Minimal target provider stub for pin service tests."""

    NAMESPACE = "anilist"

    async def fetch_nodes(self, query: NodeQuery) -> Page[Node]:
        return Page(
            items=tuple(
                Node(
                    ref=ref,
                    kind="anime",
                    title="AniBridge",
                    url="https://example.test/item",
                    labels=("dub", "favorite"),
                    facets={
                        FacetName.ARTWORK: Artwork(
                            images={"poster": "https://example.test/poster.jpg"}
                        )
                    },
                )
                for ref in query.refs
            )
        )

    async def search_nodes(
        self,
        query: str,
        *,
        limit: int = 10,
        facets: frozenset[FacetName] = frozenset(),
    ) -> Page[Node]:
        return Page(
            items=(
                Node(
                    ref=Ref.anchor("abc"),
                    kind="anime",
                    title=f"AniBridge {query}",
                    url="https://example.test/search",
                    labels=("result",),
                    facets={
                        FacetName.ARTWORK: Artwork(
                            images={"poster": "https://example.test/search.jpg"}
                        )
                    },
                ),
            )[:limit]
        )


@pytest.fixture(autouse=True)
def _pin_scheduler():
    """Attach a scheduler with a target provider for pin service tests."""
    state = get_app_state()
    original = state.scheduler
    bridge = SimpleNamespace(target_provider=DummyTargetProvider())
    state.scheduler = cast(
        SchedulerClient, SimpleNamespace(bridge_clients={"default": bridge})
    )
    yield
    state.scheduler = original


@pytest.fixture(autouse=True)
def _clear_pins():
    """Ensure the pin table is empty before and after each test."""
    with db() as ctx:
        ctx.session.query(Pin).delete()
        ctx.session.commit()
    yield
    with db() as ctx:
        ctx.session.query(Pin).delete()
        ctx.session.commit()


def _insert_pin(**overrides) -> Pin:
    now = datetime.now(UTC) - timedelta(days=1)
    media_key = overrides.get("media_key", "abc")
    pin = Pin(
        profile_name=overrides.get("profile_name", "default"),
        target_namespace=overrides.get("target_namespace", "anilist"),
        target_ref=overrides.get("target_ref", {"key": media_key, "path": []}),
        fields=overrides.get("fields", [RecordField.STATUS.value]),
        created_at=overrides.get("created_at", now),
        updated_at=overrides.get("updated_at", now),
    )
    with db() as ctx:
        ctx.session.add(pin)
        ctx.session.commit()
        ctx.session.refresh(pin)
    return pin


@pytest.mark.asyncio
async def test_pin_service_upsert_normalizes_and_validates_fields():
    """Upserts should normalize field order, dedupe entries, and reject invalid
    fields.
    """
    service = PinService()

    created = await service.upsert_pin(
        "default",
        "abc",
        [
            RecordField.STATUS,
            " progress ",
            RecordField.STATUS,
            "RATING",
        ],
    )
    assert created.fields == [
        RecordField.STATUS.value,
        RecordField.PROGRESS.value,
        RecordField.RATING.value,
    ]

    with pytest.raises(ValueError, match="Unsupported field"):
        await service.upsert_pin("default", "missing", ["missing"])

    spaced = await service.upsert_pin("default", "spaced", [" ", "status"])
    assert spaced.fields == [RecordField.STATUS.value]


def test_pin_service_lists_available_field_options():
    """Selectable pin options should expose user-facing labels."""
    options = PinService().list_options()

    assert options[0].value == RecordField.STATUS.value
    assert options[0].label == "Status"


@pytest.mark.asyncio
async def test_pin_service_lists_and_serializes_entries():
    """Return entries ordered by most recent update and serialize fields."""
    service = PinService()
    _insert_pin(media_key="1", fields=[RecordField.STATUS.value])
    newer = _insert_pin(
        media_key="2",
        fields=[RecordField.RATING.value],
        updated_at=datetime.now(UTC),
    )

    pins = await service.list_pins("default")
    assert [pin.target_ref.key for pin in pins] == ["2", "1"]
    assert pins[0].fields == [RecordField.RATING.value]

    fetched = await service.get_pin("default", cast(str, newer.target_ref["key"]))
    assert fetched is not None
    assert fetched.fields == newer.fields


@pytest.mark.asyncio
async def test_pin_service_upsert_and_delete_roundtrip():
    """Upsert pins, refresh timestamps, and delete entries cleanly."""
    service = PinService()

    created = await service.upsert_pin(
        "default",
        "abc",
        [RecordField.PROGRESS.value, RecordField.STATUS.value],
    )
    assert sorted(created.fields) == [
        RecordField.PROGRESS.value,
        RecordField.STATUS.value,
    ]

    updated = await service.upsert_pin(
        "default",
        "abc",
        [RecordField.REPEAT_COUNT.value],
    )
    assert updated.fields == [RecordField.REPEAT_COUNT.value]
    assert updated.updated_at >= created.updated_at

    service.delete_pin("default", "abc")
    assert await service.get_pin("default", "abc") is None

    with pytest.raises(ValueError):
        await service.upsert_pin("default", "xyz", [])


@pytest.mark.asyncio
async def test_pin_service_enriches_entries_with_media_metadata():
    """with_media should merge provider metadata into pin responses."""
    service = PinService()
    _insert_pin(media_key="abc", fields=[RecordField.STATUS.value])

    listed = await service.list_pins("default", with_media=True)
    fetched = await service.get_pin("default", "abc", with_media=True)
    updated = await service.upsert_pin(
        "default",
        "abc",
        [RecordField.STATUS.value],
        with_media=True,
    )

    assert listed[0].media is not None
    assert listed[0].media.title == "AniBridge"
    assert listed[0].media.labels == ["dub", "favorite"]
    assert fetched is not None and fetched.media is not None
    assert updated.media is not None


@pytest.mark.asyncio
async def test_pin_service_searches_target_and_attaches_existing_pin():
    """Target search results should include metadata and current pin state."""
    service = PinService()
    _insert_pin(media_key="abc", fields=[RecordField.STATUS.value])

    results = await service.search_pins("default", "bridge", limit=5)

    assert len(results) == 1
    assert results[0].media.key == "abc"
    assert results[0].media.title == "AniBridge bridge"
    assert results[0].media.poster_url == "https://example.test/search.jpg"
    assert results[0].pin is not None
    assert results[0].pin.fields == [RecordField.STATUS.value]


def test_pin_service_delete_missing_pin_is_noop():
    """Deleting a missing pin should quietly return."""
    PinService().delete_pin("default", "missing")
