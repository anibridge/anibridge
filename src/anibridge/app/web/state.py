"""Global web application state utilities.

Holds references to long-lived singletons (scheduler, log broadcaster, etc.) needed by
route handlers and websocket endpoints.
"""

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from anibridge.utils.cache import cache

from anibridge.app.core.anilist import AniListClient
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
        self.public_anilist: AniListClient | None = None
        self.on_shutdown_callbacks: list[Callable[[], Any]] = []
        self.started_at: datetime = datetime.now(UTC)
        self.restart_requested: bool = False

    def set_scheduler(self, scheduler: SchedulerClient) -> None:
        """Set the scheduler client.

        Args:
            scheduler (SchedulerClient): The scheduler client instance to set.
        """
        self.scheduler = scheduler

    def add_shutdown_callback(self, cb: Callable[[], Any]) -> None:
        """Register a shutdown callback executed during app shutdown.

        Args:
            cb (Callable[[], Any]): The callback function to register.
        """
        self.on_shutdown_callbacks.append(cb)

    def request_restart(self) -> None:
        """Mark that a full process restart was requested."""
        self.restart_requested = True

    async def ensure_public_anilist(self) -> AniListClient:
        """Get or create the shared public AniList client.

        Returns:
            AniListClient: A tokenless AniList client suitable for public queries.
        """
        if self.public_anilist is None:
            self.public_anilist = AniListClient(anilist_token=None)
            await self.public_anilist.initialize()
        return self.public_anilist

    async def shutdown(self) -> None:
        """Run registered shutdown callbacks (ignore individual errors).

        Args:
            self (AppState): The application state instance.
        """
        for cb in self.on_shutdown_callbacks:
            try:
                res = cb()
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                pass

        if self.public_anilist is not None:
            with suppress(Exception):
                await self.public_anilist.close()
            self.public_anilist = None


@cache
def get_app_state() -> AppState:
    """Get the singleton application state instance.

    Returns:
        AppState: The application state instance.
    """
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
