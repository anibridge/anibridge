"""Tests for the global AppState helper."""

import asyncio

import pytest

from anibridge.app.web.state import get_app_state


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
        "anibridge.app.web.state.AniListClient", lambda anilist_token=None: dummy
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
