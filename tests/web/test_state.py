"""Tests for the global AppState helper."""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from anibridge.app.core.sched.client import SchedulerClient
from anibridge.app.exceptions import ProfileNotFoundError, SchedulerNotInitializedError
from anibridge.app.web.state import get_app_state, get_bridge


class DummyAniListClient:
    """Test double that records initialize/close calls."""

    def __init__(self) -> None:
        """Set up state tracking flags for test assertions."""
        self.initialized = False
        self.closed = False

    async def initialize(self) -> None:
        """Mark the client as initialized."""
        self.initialized = True

    async def close(self) -> None:
        """Mark the client as closed."""
        self.closed = True


@pytest.mark.asyncio
async def test_app_state_public_anilist_lifecycle(monkeypatch: pytest.MonkeyPatch):
    """ensure_public_anilist caches the client and shutdown closes it."""
    dummy = DummyAniListClient()
    monkeypatch.setattr(
        "anibridge.app.web.state.AnilistClient", lambda anilist_token=None: dummy
    )

    get_app_state.cache_clear()
    state = get_app_state()

    client = await state.ensure_public_anilist()
    assert client is dummy
    assert dummy.initialized is True

    await state.shutdown()
    assert dummy.closed is True
    assert state.public_anilist is None


@pytest.mark.asyncio
async def test_app_state_shutdown_callbacks_can_be_sync_or_async():
    """Registered shutdown callbacks support sync and async callables."""
    get_app_state.cache_clear()
    state = get_app_state()

    called: list[str] = []

    def sync_cb() -> None:
        called.append("sync")

    async def async_cb() -> None:
        await asyncio.sleep(0)
        called.append("async")

    state.add_shutdown_callback(sync_cb)
    state.add_shutdown_callback(async_cb)

    await state.shutdown()
    assert called == ["sync", "async"]


@pytest.mark.asyncio
async def test_app_state_status_change_wait_and_timeout() -> None:
    """Status notifications should wake waiters and timeouts should be harmless."""
    state = get_app_state()

    state.notify_status_change()
    await state.wait_status_change(0.1)
    assert state._status_changed.is_set() is False

    await state.wait_status_change(0.01)
    assert state._status_changed.is_set() is False


@pytest.mark.asyncio
async def test_app_state_shutdown_ignores_callback_and_close_errors() -> None:
    """Shutdown should keep going after individual callback/client failures."""
    get_app_state.cache_clear()
    state = get_app_state()
    called: list[str] = []

    def broken_cb() -> None:
        called.append("broken")
        raise RuntimeError("boom")

    async def async_cb() -> None:
        called.append("async")

    class BrokenClient(DummyAniListClient):
        async def close(self) -> None:
            raise RuntimeError("close failed")

    state.add_shutdown_callback(broken_cb)
    state.add_shutdown_callback(async_cb)
    state.public_anilist = BrokenClient()  # ty:ignore[invalid-assignment]

    await state.shutdown()

    assert called == ["broken", "async"]
    assert state.public_anilist is None


def test_app_state_scheduler_and_restart_helpers() -> None:
    """Scheduler/restart helpers should mutate state directly."""
    get_app_state.cache_clear()
    state = get_app_state()
    scheduler = SimpleNamespace(bridge_clients={})

    state.set_scheduler(scheduler)  # ty:ignore[invalid-argument-type]
    state.request_restart()

    assert state.scheduler is scheduler
    assert state.restart_requested is True


def test_get_bridge_validates_scheduler_and_profile() -> None:
    """Bridge lookup should surface unavailable scheduler/profile states."""
    get_app_state.cache_clear()
    state = get_app_state()

    with pytest.raises(SchedulerNotInitializedError):
        get_bridge("missing")

    bridge = object()
    state.scheduler = cast(
        SchedulerClient,
        SimpleNamespace(bridge_clients={"default": bridge}),
    )

    assert get_bridge("default") is bridge
    with pytest.raises(ProfileNotFoundError):
        get_bridge("missing")
