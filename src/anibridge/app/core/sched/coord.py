"""Global scheduling coordination primitives."""

import asyncio
from collections.abc import Awaitable, Callable


class GlobalSyncCoordinator:
    """Serialize maintenance with profile sync activity."""

    def __init__(self) -> None:
        """Initialize coordinator counters and synchronization state."""
        self._condition = asyncio.Condition()
        self._active_profile_syncs = 0
        self._maintenance_active = False
        self._maintenance_waiting = 0

    async def acquire_profile_slot(self, _profile_name: str) -> None:
        """Block profile sync starts while maintenance is active or pending."""
        async with self._condition:
            while self._maintenance_active or self._maintenance_waiting > 0:
                await self._condition.wait()
            self._active_profile_syncs += 1

    async def release_profile_slot(self, _profile_name: str) -> None:
        """Release an active profile sync slot."""
        async with self._condition:
            if self._active_profile_syncs > 0:
                self._active_profile_syncs -= 1
            self._condition.notify_all()

    async def run_maintenance(self, work: Callable[[], Awaitable[None]]) -> None:
        """Execute maintenance work with exclusive access against profile syncs."""
        async with self._condition:
            self._maintenance_waiting += 1
            try:
                while self._maintenance_active or self._active_profile_syncs > 0:
                    await self._condition.wait()
                self._maintenance_active = True
            finally:
                self._maintenance_waiting -= 1

        try:
            await work()
        finally:
            async with self._condition:
                self._maintenance_active = False
                self._condition.notify_all()

    async def get_metrics(self) -> dict[str, int | bool]:
        """Expose coordinator counters for diagnostics."""
        async with self._condition:
            return {
                "active_profile_syncs": self._active_profile_syncs,
                "maintenance_active": self._maintenance_active,
                "maintenance_waiting": self._maintenance_waiting,
            }
