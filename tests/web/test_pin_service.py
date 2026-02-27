"""Unit tests for the pin management service."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from anibridge.app.config.database import db
from anibridge.app.config.settings import SyncField
from anibridge.app.core.sched import SchedulerClient
from anibridge.app.models.db.pin import Pin
from anibridge.app.web.services.pin_service import PinService, UpdatePinPayload
from anibridge.app.web.state import get_app_state


class DummyListProvider:
    """Minimal list provider stub for pin service tests."""

    NAMESPACE = "anilist"

    async def get_entries_batch(self, keys):
        return []


@pytest.fixture(autouse=True)
def _pin_scheduler():
    """Attach a scheduler with a list provider for pin service tests."""
    state = get_app_state()
    original = state.scheduler
    bridge = SimpleNamespace(list_provider=DummyListProvider())
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
    pin = Pin(
        profile_name=overrides.get("profile_name", "default"),
        list_namespace=overrides.get("list_namespace", "anilist"),
        list_media_key=overrides.get("list_media_key", "abc"),
        fields=overrides.get("fields", [SyncField.STATUS.value]),
        created_at=overrides.get("created_at", now),
        updated_at=overrides.get("updated_at", now),
    )
    with db() as ctx:
        ctx.session.add(pin)
        ctx.session.commit()
        ctx.session.refresh(pin)
    return pin


def test_update_pin_payload_normalizes_and_validates():
    """Normalize payloads into SyncField order and reject invalid fields."""
    payload = UpdatePinPayload(
        fields=[
            SyncField.STATUS,
            " progress ",
            SyncField.STATUS,  # duplicate
            "USER_RATING",
        ]
    )
    assert payload.normalized() == [
        SyncField.STATUS.value,
        SyncField.PROGRESS.value,
        SyncField.USER_RATING.value,
    ]

    with pytest.raises(ValueError, match="Unsupported field"):
        UpdatePinPayload(fields=["missing"]).normalized()


@pytest.mark.asyncio
async def test_pin_service_lists_and_serializes_entries():
    """Return entries ordered by most recent update and serialize fields."""
    service = PinService()
    _insert_pin(list_media_key="1", fields=[SyncField.STATUS.value])
    newer = _insert_pin(
        list_media_key="2",
        fields=[SyncField.USER_RATING.value],
        updated_at=datetime.now(UTC),
    )

    pins = await service.list_pins("default")
    assert [pin.list_media_key for pin in pins] == ["2", "1"]
    assert pins[0].fields == [SyncField.USER_RATING.value]

    fetched = await service.get_pin("default", newer.list_media_key)
    assert fetched is not None
    assert fetched.fields == newer.fields


@pytest.mark.asyncio
async def test_pin_service_upsert_and_delete_roundtrip():
    """Upsert pins, refresh timestamps, and delete entries cleanly."""
    service = PinService()

    created = await service.upsert_pin(
        "default",
        "abc",
        [SyncField.PROGRESS.value, SyncField.STATUS.value],
    )
    assert sorted(created.fields) == [SyncField.PROGRESS.value, SyncField.STATUS.value]

    updated = await service.upsert_pin(
        "default",
        "abc",
        [SyncField.REPEATS.value],
    )
    assert updated.fields == [SyncField.REPEATS.value]
    assert updated.updated_at >= created.updated_at

    service.delete_pin("default", "abc")
    assert await service.get_pin("default", "abc") is None

    with pytest.raises(ValueError):
        await service.upsert_pin("default", "xyz", [])
