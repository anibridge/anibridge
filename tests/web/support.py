"""Shared test doubles for web-layer test suites."""

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from anibridge.app.core.sync.request import SyncRequest


class SchedulerStub:
    """Flexible scheduler double shared across web tests."""

    def __init__(
        self,
        *,
        running: bool = False,
        status_payload: dict[str, Any] | None = None,
        runtime_metrics: dict[str, Any] | None = None,
        profiles: dict[str, object] | None = None,
        bridge_clients: dict[str, object] | None = None,
        failed_profile_errors: dict[str, str] | None = None,
        global_config: Any | None = None,
        next_database_sync_at: datetime | None = None,
    ) -> None:
        self._running = running
        self.initialized = False
        self.started = False
        self.stopped = False
        self.shutdown_requested = False
        self.reinitialized_profiles: list[str] = []
        self.profile_sync_calls: list[tuple[str, SyncRequest, str]] = []
        self.all_sync_calls: list[tuple[SyncRequest, str]] = []
        self.database_sync_calls: list[str] = []
        self._status_payload = status_payload or {}
        self._runtime_metrics = runtime_metrics or {}
        self._next_database_sync_at = next_database_sync_at
        self.bridge_clients = dict(bridge_clients or {})
        self.failed_profile_errors = dict(failed_profile_errors or {})
        self.global_config = global_config or SimpleNamespace(
            profiles=dict(profiles or {})
        )

    @property
    def is_running(self) -> bool:
        return self._running

    async def initialize(self) -> None:
        self.initialized = True

    async def start(self) -> None:
        self.started = True
        self._running = True

    async def stop(self) -> None:
        self.stopped = True
        self._running = False

    def request_shutdown(self) -> None:
        self.shutdown_requested = True

    async def get_status(self) -> dict[str, Any]:
        return self._status_payload

    async def get_runtime_metrics(self) -> dict[str, Any]:
        return self._runtime_metrics

    def get_next_database_sync_at(self) -> datetime | None:
        return self._next_database_sync_at

    async def reinitialize_profile(self, profile: str) -> None:
        self.reinitialized_profiles.append(profile)

    async def trigger_all_profiles_sync(
        self, *, request: SyncRequest, source: str
    ) -> None:
        self.all_sync_calls.append((request, source))

    async def trigger_database_sync(self, *, source: str) -> None:
        self.database_sync_calls.append(source)

    async def trigger_profile_sync(
        self,
        profile: str,
        *,
        request: SyncRequest,
        source: str,
    ) -> None:
        self.profile_sync_calls.append((profile, request, source))
