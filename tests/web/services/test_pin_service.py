"""Unit tests for the pin management service."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from anibridge.provider.base import (
    Artwork,
    Event,
    FacetName,
    Node,
    NodeQuery,
    Page,
    Query,
    Record,
    Ref,
    SupportsNodeSearch,
    SupportsReads,
)

from anibridge.app.config.database import db
from anibridge.app.core.sched.client import SchedulerClient
from anibridge.app.core.sync import RefPayload, RefStepPayload
from anibridge.app.models.db.pin import Pin
from anibridge.app.web.services.pin_service import PinService
from anibridge.app.web.state import get_app_state


class DummyTargetProvider(SupportsReads, SupportsNodeSearch):
    """Minimal target provider stub for pin service tests."""

    NAMESPACE = "anilist"

    async def fetch(self, query: Query) -> Page[Node | Record | Event]:
        assert isinstance(query, NodeQuery)
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
def _pin_scheduler(monkeypatch: pytest.MonkeyPatch):
    """Attach a scheduler with a target provider for pin service tests."""
    monkeypatch.setattr(
        "anibridge.app.web.services.pin_service.get_config",
        lambda: SimpleNamespace(
            get_profile=lambda _: SimpleNamespace(target_provider="anilist")
        ),
    )
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
        target_parent_ref=overrides.get(
            "target_parent_ref", {"key": media_key, "path": []}
        ),
        created_at=overrides.get("created_at", now),
        updated_at=overrides.get("updated_at", now),
    )
    with db() as ctx:
        ctx.session.add(pin)
        ctx.session.commit()
        ctx.session.refresh(pin)
    return pin


@pytest.mark.asyncio
async def test_pin_service_upsert_creates_parent_pin():
    """Upserts should create a parent-level pin without field payloads."""
    service = PinService()

    created = await service.upsert_pin("default", "abc")

    assert created.target_parent_ref.key == "abc"
    assert created.target_parent_ref.path == ()


@pytest.mark.asyncio
async def test_pin_service_lists_and_serializes_entries():
    """Return entries ordered by most recent update and serialize parent refs."""
    service = PinService()
    _insert_pin(media_key="1")
    newer = _insert_pin(
        media_key="2",
        updated_at=datetime.now(UTC),
    )

    pins = await service.list_pins("default")
    assert [pin.target_parent_ref.key for pin in pins] == ["2", "1"]

    fetched = await service.get_pin(
        "default", cast(str, newer.target_parent_ref["key"])
    )
    assert fetched is not None
    assert fetched.target_parent_ref.key == "2"


@pytest.mark.asyncio
async def test_pin_service_upsert_and_delete_roundtrip():
    """Upsert pins, refresh timestamps, and delete entries cleanly."""
    service = PinService()

    created = await service.upsert_pin("default", "abc")
    assert created.target_parent_ref.key == "abc"

    updated = await service.upsert_pin("default", "abc")
    assert updated.updated_at >= created.updated_at

    service.delete_pin("default", "abc")
    assert await service.get_pin("default", "abc") is None


@pytest.mark.asyncio
async def test_pin_service_preserves_pathful_target_refs():
    """Pins for the same key but different provider paths remain independent."""
    service = PinService()
    first_ref = RefPayload("abc", (RefStepPayload("episode", 1),))
    second_ref = RefPayload("abc", (RefStepPayload("episode", 2),))

    first = await service.upsert_pin("default", "abc", target_ref=first_ref)
    second = await service.upsert_pin("default", "abc", target_ref=second_ref)

    assert first.target_parent_ref == first_ref
    assert second.target_parent_ref == second_ref
    assert [pin.target_parent_ref for pin in await service.list_pins("default")] == [
        second_ref,
        first_ref,
    ]

    service.delete_pin("default", "abc", target_ref=first_ref)

    assert await service.get_pin("default", "abc", target_ref=first_ref) is None
    assert await service.get_pin("default", "abc", target_ref=second_ref) is not None


@pytest.mark.asyncio
async def test_pin_service_plain_crud_uses_config_without_scheduler():
    """Plain pin CRUD should not require active provider clients."""
    get_app_state().scheduler = None
    service = PinService()

    created = await service.upsert_pin("default", "offline")

    assert created.target_namespace == "anilist"
    assert [
        pin.target_parent_ref.key for pin in await service.list_pins("default")
    ] == ["offline"]
    assert await service.get_pin("default", "offline") is not None

    service.delete_pin("default", "offline")
    assert await service.get_pin("default", "offline") is None


@pytest.mark.asyncio
async def test_pin_service_enriches_entries_with_media_metadata():
    """with_media should merge provider metadata into pin responses."""
    service = PinService()
    _insert_pin(media_key="abc")

    listed = await service.list_pins("default", with_media=True)
    fetched = await service.get_pin("default", "abc", with_media=True)
    updated = await service.upsert_pin("default", "abc", with_media=True)

    assert listed[0].media is not None
    assert listed[0].media.title == "AniBridge"
    assert listed[0].media.labels == ["dub", "favorite"]
    assert fetched is not None and fetched.media is not None
    assert updated.media is not None


@pytest.mark.asyncio
async def test_pin_service_searches_target_and_attaches_existing_pin():
    """Target search results should include metadata and current pin state."""
    service = PinService()
    _insert_pin(media_key="abc")

    results = await service.search_pins("default", "bridge", limit=5)

    assert len(results) == 1
    assert results[0].media.key == "abc"
    assert results[0].media.title == "AniBridge bridge"
    assert results[0].media.poster_url == "https://example.test/search.jpg"
    assert results[0].pin is not None
    assert results[0].pin.target_parent_ref.key == "abc"


def test_pin_service_delete_missing_pin_is_noop():
    """Deleting a missing pin should quietly return."""
    PinService().delete_pin("default", "missing")
