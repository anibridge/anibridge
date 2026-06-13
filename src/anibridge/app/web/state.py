"""Global web application state utilities.

Holds references to long-lived singletons (scheduler, log broadcaster, etc.) needed by
route handlers and websocket endpoints.
"""

import asyncio
import inspect
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from anibridge.utils.cache import cache

from anibridge.app.core.anilist import AnilistClient
from anibridge.app.exceptions import ProfileNotFoundError, SchedulerNotInitializedError

__all__ = ["AppState", "get_app_state", "get_bridge"]

if TYPE_CHECKING:
    from anibridge.app.core.bridge import BridgeClient
    from anibridge.app.core.sched import SchedulerClient


class AppState:
    """Container for global web application state."""

    def __init__(self) -> None:
        """Initialize empty state containers and record process start time."""
        self.scheduler: SchedulerClient | None = None
        self.public_anilist: AnilistClient | None = None
        self.on_shutdown_callbacks: list[Callable[[], Any]] = []
        self.started_at: datetime = datetime.now(UTC)
        self.restart_requested: bool = False
        self._status_changed: asyncio.Event = asyncio.Event()

    def notify_status_change(self) -> None:
        """Signal that scheduler/profile status has changed.

        Wakes any WebSocket handlers waiting on `wait_status_change`.
        """
        self._status_changed.set()

    async def wait_status_change(self, max_wait: float) -> None:
        """Wait up to `max_wait` seconds for a status change notification.

        After waking (or on timeout) the internal flag is cleared so the next
        call blocks again until a new notification arrives.
        """
        with suppress(TimeoutError):
            await asyncio.wait_for(self._status_changed.wait(), timeout=max_wait)
        self._status_changed.clear()

    def set_scheduler(self, scheduler: SchedulerClient) -> None:
        """Set the scheduler client."""
        self.scheduler = scheduler

    def add_shutdown_callback(self, cb: Callable[[], Any]) -> None:
        """Register a shutdown callback executed during app shutdown."""
        self.on_shutdown_callbacks.append(cb)

    def request_restart(self) -> None:
        """Mark that a full process restart was requested."""
        self.restart_requested = True

    async def ensure_public_anilist(self) -> AnilistClient:
        """Get or create the shared public AniList client."""
        if self.public_anilist is None:
            self.public_anilist = AnilistClient(anilist_token=None)
            await self.public_anilist.initialize()
        return self.public_anilist

    async def shutdown(self) -> None:
        """Run registered shutdown callbacks (ignore individual errors)."""
        for cb in self.on_shutdown_callbacks:
            try:
                res = cb()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                pass

        if self.public_anilist is not None:
            with suppress(Exception):
                await self.public_anilist.close()
            self.public_anilist = None


@cache
def get_app_state() -> AppState:
    """Get the singleton application state instance."""
    return AppState()


def get_bridge(profile: str) -> BridgeClient:
    """Return the bridge client for a specific profile."""
    scheduler = get_app_state().scheduler
    if not scheduler:
        raise SchedulerNotInitializedError("Scheduler not available")
    bridge = scheduler.bridge_clients.get(profile)
    if not bridge:
        raise ProfileNotFoundError(f"Unknown profile: {profile}")
    return bridge
