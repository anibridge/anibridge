"""Scheduler Module."""

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import msgspec
from anibridge.utils.cache import lru_cache

from anibridge.app import log
from anibridge.app.config.settings import AnibridgeConfig, ScanMode
from anibridge.app.core.animap import AnimapClient
from anibridge.app.core.bridge import BridgeClient
from anibridge.app.core.sched.coord import GlobalSyncCoordinator
from anibridge.app.core.sched.profile import ProfileScheduler
from anibridge.app.exceptions import ProfileNotFoundError, SchedulerUnavailableError
from anibridge.app.utils.cron import format_interval, is_enabled_interval
from anibridge.app.utils.human import human_duration
from anibridge.app.utils.memory import release_memory

__all__ = ["SchedulerClient"]

_MAINTENANCE_TIMEOUT: float = 3600


class SchedulerClient:
    """Application scheduler that manages all profiles and global tasks.

    Coordinates multiple profile schedulers and handles shared resources like
    the daily database sync. Provides centralized management and graceful shutdown.
    """

    def __init__(self, global_config: AnibridgeConfig):
        """Initialize the application scheduler.

        Args:
            global_config (AnibridgeConfig): Global application configuration.
        """
        self.global_config = global_config
        self.shared_animap_client = AnimapClient(
            global_config.data_path, global_config.mappings_url
        )
        self.bridge_clients: dict[str, BridgeClient] = {}
        self.failed_profile_errors: dict[str, str] = {}
        self.profile_schedulers: dict[str, ProfileScheduler] = {}
        self._sync_coordinator = GlobalSyncCoordinator()
        self.stop_event = asyncio.Event()
        self._running = False
        self._daily_sync_task: asyncio.Task | None = None

    def request_shutdown(self) -> None:
        """Request application shutdown from external callers."""
        if not self.stop_event.is_set():
            self.stop_event.set()

    @property
    def is_running(self) -> bool:
        """Return hether the scheduler main loop is currently running."""
        return self._running

    def get_next_database_sync_at(self) -> datetime | None:
        """Get the next scheduled database sync time in UTC.

        Returns:
            datetime | None: The next scheduled database sync time in UTC, or None if
                the scheduler is not running.
        """
        if not self._running:
            return None
        now = datetime.now(UTC)
        return self._get_next_1am_utc(now)

    async def initialize(self) -> None:
        """Initialize the application scheduler and all components."""
        log.info("Initializing application scheduler")

        log.info("Initializing anime mapping database")
        await self.shared_animap_client.initialize()
        log.success("Anime mapping database ready")
        self.failed_profile_errors.clear()

        async def init_bridge(profile_name: str, profile_config: Any) -> None:
            try:
                bridge_client = await self._initialize_bridge_client(
                    profile_name, profile_config
                )
                self.bridge_clients[profile_name] = bridge_client
                self.failed_profile_errors.pop(profile_name, None)
            except Exception as exc:
                detail = str(exc).strip() or "Failed to initialize profile"
                self.failed_profile_errors[profile_name] = detail

        initialize_tasks: list[asyncio.Task] = []
        for profile_name, profile_config in self.global_config.profiles.items():
            initialize_tasks.append(
                asyncio.create_task(init_bridge(profile_name, profile_config))
            )
        if initialize_tasks:
            await asyncio.gather(*initialize_tasks)

        release_memory()

        log.info(
            "Application scheduler initialized with %s profile(s)",
            len(self.bridge_clients),
        )

    async def _initialize_bridge_client(
        self, profile_name: str, profile_config: Any
    ) -> BridgeClient:
        """Build and initialize a bridge client for the given profile."""
        log.info("[%s] Setting up bridge client", profile_name)

        bridge_client: BridgeClient | None = None
        try:
            bridge_client = BridgeClient(
                profile_name=profile_name,
                profile_config=profile_config,
                global_config=self.global_config,
                shared_animap_client=self.shared_animap_client,
            )
            await bridge_client.initialize()
        except Exception:
            log.error("[%s] Bridge client setup failed", profile_name)
            log.exception(
                "[%s] Bridge setup error details",
                profile_name,
            )
            if bridge_client is not None:
                with contextlib.suppress(Exception):
                    await bridge_client.close()
            raise

        log.info("[%s] Bridge client initialized", profile_name)
        return bridge_client

    async def _start_profile_scheduler(
        self, profile_name: str, bridge_client: BridgeClient
    ) -> None:
        """Create and start the runtime scheduler for a single profile."""
        profile_config = self.global_config.get_profile(profile_name)

        log.info(
            "[%s] Starting scheduler: poll_interval=%s, scan_interval=%s, "
            "modes=%s, full_scan=%s, destructive=%s",
            profile_name,
            format_interval(profile_config.poll_interval),
            format_interval(profile_config.scan_interval),
            profile_config.scan_modes,
            "enabled" if profile_config.full_scan else "disabled",
            "enabled" if profile_config.destructive_sync else "disabled",
        )

        scheduler = ProfileScheduler(
            profile_name=profile_name,
            bridge_client=bridge_client,
            poll_interval=profile_config.poll_interval,
            scan_interval=profile_config.scan_interval,
            scan_modes=profile_config.scan_modes,
            before_sync=self._sync_coordinator.acquire_profile_slot,
            after_sync=self._sync_coordinator.release_profile_slot,
            stop_event=self.stop_event,
        )

        self.profile_schedulers[profile_name] = scheduler
        await scheduler.start()

        if profile_config.scan_modes:
            next_sync_time = "in progress"
            if ScanMode.PERIODIC in profile_config.scan_modes and is_enabled_interval(
                profile_config.scan_interval
            ):
                next_sync = datetime.now(UTC).astimezone()
                next_sync_time = "at {}".format(next_sync.strftime("%Y-%m-%d %H:%M:%S"))

            log.info(
                "[%s] Scheduler started, next sync: %s",
                profile_name,
                next_sync_time,
            )

    async def start(self) -> None:
        """Start all profile schedulers and global tasks."""
        if self._running:
            return

        if self.stop_event.is_set():
            self.stop_event = asyncio.Event()

        self._running = True

        log.info("Starting application scheduler")

        self._daily_sync_task = asyncio.create_task(self._daily_db_sync_loop())

        for profile_name, bridge_client in self.bridge_clients.items():
            await self._start_profile_scheduler(profile_name, bridge_client)

        if self.profile_schedulers and all(
            not self.global_config.get_profile(name).scan_modes
            for name in self.profile_schedulers
        ):
            log.info(
                "None of the profiles have any scan modes enabled; the scheduler will "
                "remain idle until manually triggered",
            )

        if self.profile_schedulers:
            log.info(
                "Application scheduler started with %s profile(s)",
                len(self.profile_schedulers),
            )
        else:
            log.warning("No profile schedulers were started")

    async def stop(self) -> None:
        """Stop all schedulers and clean up resources."""
        if not self._running:
            return

        self._running = False

        log.info("Stopping application scheduler")

        self.stop_event.set()

        if self._daily_sync_task and not self._daily_sync_task.done():
            self._daily_sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._daily_sync_task

        stop_tasks = []
        for profile_name, scheduler in self.profile_schedulers.items():
            log.debug("[%s] Stopping scheduler", profile_name)
            stop_tasks.append(scheduler.stop())

        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)

        close_tasks = []
        for profile_name, bridge_client in self.bridge_clients.items():
            log.debug("[%s] Closing bridge client", profile_name)
            close_tasks.append(bridge_client.close())

        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)

        await self.shared_animap_client.close()

        self.profile_schedulers.clear()
        self.bridge_clients.clear()
        self.get_profiles_for_library_provider.cache_clear()

        log.info("Application scheduler stopped")

    async def wait_for_completion(self) -> None:
        """Wait for the application to complete or be stopped."""
        if not self._running:
            return

        try:
            await self.stop_event.wait()
        except asyncio.CancelledError:
            log.info("Application scheduler wait interrupted")
            raise

    async def trigger_profile_sync(
        self,
        profile_name: str,
        poll: bool = False,
        library_keys: Sequence[str] | None = None,
        source: str = "manual",
    ) -> None:
        """Trigger a sync for a single profile.

        Args:
            profile_name (str): Specific profile to sync.
            poll (bool): Whether to use polling mode for the sync.
            library_keys (Sequence[str] | None): Optional library media keys to scope.
            source (str): Origin of the trigger request.

        Raises:
            ProfileNotFoundError: If the specified profile does not exist.
        """
        if profile_name not in self.bridge_clients:
            raise ProfileNotFoundError(f"Profile '{profile_name}' not found")

        log.info(
            "[%s] Triggering sync (poll=%s, source=%s)",
            profile_name,
            poll,
            source,
        )
        scheduler = self.profile_schedulers.get(profile_name)
        if scheduler is None:
            await self._sync_profile_once(
                profile_name=profile_name,
                poll=poll,
                library_keys=library_keys,
            )
            return

        await scheduler.sync(poll=poll, library_keys=library_keys, source=source)

    async def trigger_all_profiles_sync(
        self,
        poll: bool = False,
        library_keys: Sequence[str] | None = None,
        source: str = "manual",
    ) -> None:
        """Trigger a sync for all profiles."""
        log.info(
            "Triggering sync for all profiles (poll=%s, source=%s)",
            poll,
            source,
        )
        profile_names = tuple(self.bridge_clients)
        if not profile_names:
            log.warning("No profiles available to sync")
            return

        sync_tasks = []
        for name in profile_names:
            log.info("[%s] Triggering sync", name)
            sync_tasks.append(
                self.trigger_profile_sync(
                    profile_name=name,
                    poll=poll,
                    library_keys=library_keys,
                    source=source,
                )
            )

        if sync_tasks:
            results = await asyncio.gather(*sync_tasks, return_exceptions=True)
            exceptions: list[Exception] = []
            for profile_name, result in zip(profile_names, results, strict=False):
                if isinstance(result, Exception):
                    log.error(
                        "[%s] Profile sync trigger failed: %s", profile_name, result
                    )
                    exceptions.append(result)

            if exceptions:
                raise ExceptionGroup(
                    "One or more profile sync triggers failed",
                    exceptions,
                )

    async def get_status(self) -> dict[str, dict[str, Any]]:
        """Get the status of all profiles.

        Returns:
            dict[str, dict[str, Any]]: A dictionary containing the profile info.
        """
        status = {}

        for profile_name, profile_config in self.global_config.profiles.items():
            bridge_client = self.bridge_clients.get(profile_name)
            scheduler = self.profile_schedulers.get(profile_name)

            library_namespace: str | None = None
            list_namespace: str | None = None
            library_user_title: str | None = None
            list_user_title: str | None = None

            if bridge_client is not None:
                library_namespace = bridge_client.library_provider.NAMESPACE.title()
                list_namespace = bridge_client.list_provider.NAMESPACE.title()

                library_user = bridge_client.library_provider.user()
                if library_user is not None:
                    library_user_title = library_user.title

                list_user = bridge_client.list_provider.user()
                if list_user is not None:
                    list_user_title = list_user.title

            status[profile_name] = {
                "config": {
                    "library_namespace": library_namespace
                    or profile_config.library_provider.title(),
                    "list_namespace": list_namespace
                    or profile_config.list_provider.title(),
                    "library_user": library_user_title,
                    "list_user": list_user_title,
                    "scan_interval": profile_config.scan_interval,
                    "poll_interval": profile_config.poll_interval,
                    "scan_modes": [m.value for m in profile_config.scan_modes],
                    "full_scan": profile_config.full_scan,
                    "destructive_sync": profile_config.destructive_sync,
                },
                "status": {
                    "running": scheduler is not None and scheduler._running
                    if scheduler
                    else False,
                    "last_synced": bridge_client.last_synced.isoformat()
                    if bridge_client and bridge_client.last_synced
                    else None,
                    "current_sync": (
                        msgspec.to_builtins(bridge_client.current_sync)
                        if bridge_client and bridge_client.current_sync is not None
                        else None
                    ),
                    "initialization_error": self.failed_profile_errors.get(
                        profile_name
                    ),
                    "scheduler": await scheduler.get_runtime_metrics()
                    if scheduler is not None
                    else None,
                },
            }

        return status

    async def get_runtime_metrics(self) -> dict[str, Any]:
        """Return scheduler-level runtime metrics and coordinator state."""
        return {
            "running": self._running,
            "profile_count": len(self.profile_schedulers),
            "bridge_count": len(self.bridge_clients),
            "daily_sync_active": self._daily_sync_task is not None
            and not self._daily_sync_task.done(),
            "coordinator": self._sync_coordinator.get_metrics(),
        }

    async def trigger_database_sync(self, source: str = "manual:database") -> None:
        """Trigger a globally coordinated database sync and daily profile backups."""

        async def _sync_and_backup() -> None:
            await self.shared_animap_client.sync_db()
            log.success("Database sync completed (source=%s)", source)

            log.info("Starting daily list provider backups")
            backup_tasks = []
            profile_names = []
            for profile_name, bridge_client in self.bridge_clients.items():
                backup_tasks.append(bridge_client._backup_list())
                profile_names.append(profile_name)

            if not backup_tasks:
                return

            results = await asyncio.gather(*backup_tasks, return_exceptions=True)
            exceptions: list[Exception] = []
            for profile_name, result in zip(profile_names, results, strict=False):
                if isinstance(result, Exception):
                    log.error(
                        "[%s] List backup failed: %s",
                        profile_name,
                        result,
                    )
                    exceptions.append(result)

            if exceptions:
                raise ExceptionGroup(
                    "One or more daily profile backups failed",
                    exceptions,
                )

        log.info("Starting database sync (source=%s)", source)
        try:
            await self._sync_coordinator.run_maintenance(
                _sync_and_backup, timeout_=_MAINTENANCE_TIMEOUT
            )
        except TimeoutError:
            log.error(
                "Database sync timed out after %d seconds; "
                "maintenance lock released (source=%s)",
                _MAINTENANCE_TIMEOUT,
                source,
            )
            raise
        release_memory()

    def _get_next_1am_utc(self, now: datetime) -> datetime:
        """Calculate the next 1:00 AM UTC, handling DST transitions properly.

        Args:
            now: Current UTC datetime

        Returns:
            datetime: Next 1:00 AM UTC
        """
        candidate = now.replace(hour=1, minute=0, second=0, microsecond=0)
        if now >= candidate:
            candidate += timedelta(days=1)
        return candidate

    async def _daily_db_sync_loop(self) -> None:
        """Handle daily database synchronization at 1:00 AM UTC."""
        log.info("Starting daily database sync scheduler")

        while self._running and not self.stop_event.is_set():
            try:
                now = datetime.now(UTC)
                next_sync_time = self._get_next_1am_utc(now)

                sleep_duration = int((next_sync_time - now).total_seconds())

                log.info(
                    "Next database sync scheduled for: %s (in %s)",
                    next_sync_time.astimezone(),
                    human_duration(sleep_duration),
                )

                try:
                    await asyncio.wait_for(self.stop_event.wait(), sleep_duration)
                    break
                except TimeoutError:
                    pass

                if not self._running or self.stop_event.is_set():
                    break

                log.info("Starting daily database sync")
                try:
                    await self.trigger_database_sync(source="loop:daily_db")
                except Exception as e:
                    log.error("Daily database sync failed: %s", e)
                    log.exception("Daily database sync error details")

            except asyncio.CancelledError:
                log.debug("Daily database sync cancelled")
                break
            except Exception:
                log.error("Daily database sync error")
                log.exception("Daily database sync loop error details")
                await asyncio.sleep(3600)  # Retry after 1 hour on error

        log.info("Daily database sync scheduler stopped")

    async def reinitialize_profile(self, profile_name: str) -> None:
        """Rebuild and restart a single profile bridge and scheduler."""
        if profile_name not in self.global_config.profiles:
            raise ProfileNotFoundError(f"Profile '{profile_name}' not found")

        async def _reinitialize() -> None:
            log.info("[%s] Reinitializing profile", profile_name)

            existing_scheduler = self.profile_schedulers.pop(profile_name, None)
            if existing_scheduler is not None:
                await existing_scheduler.stop(set_stop_event=False)

            existing_bridge = self.bridge_clients.pop(profile_name, None)
            if existing_bridge is not None:
                await existing_bridge.close()

            profile_config = self.global_config.get_profile(profile_name)
            try:
                bridge_client = await self._initialize_bridge_client(
                    profile_name, profile_config
                )
            except Exception as exc:
                detail = str(exc).strip() or "Failed to initialize profile"
                self.failed_profile_errors[profile_name] = detail
                raise SchedulerUnavailableError(
                    f"Failed to reinitialize profile '{profile_name}': {detail}"
                ) from exc

            self.bridge_clients[profile_name] = bridge_client
            self.failed_profile_errors.pop(profile_name, None)

            if self._running:
                await self._start_profile_scheduler(profile_name, bridge_client)

            self.get_profiles_for_library_provider.cache_clear()
            log.success("[%s] Profile reinitialized successfully", profile_name)

        await self._sync_coordinator.run_maintenance(
            _reinitialize, timeout_=_MAINTENANCE_TIMEOUT
        )

    async def remove_profile(self, profile_name: str) -> None:
        """Stop and remove a single profile bridge and scheduler."""

        async def _remove() -> None:
            log.info("[%s] Removing profile from runtime scheduler", profile_name)

            existing_scheduler = self.profile_schedulers.pop(profile_name, None)
            if existing_scheduler is not None:
                await existing_scheduler.stop(set_stop_event=False)

            existing_bridge = self.bridge_clients.pop(profile_name, None)
            if existing_bridge is not None:
                await existing_bridge.close()

            self.failed_profile_errors.pop(profile_name, None)
            self.get_profiles_for_library_provider.cache_clear()
            log.success("[%s] Profile removed from runtime scheduler", profile_name)

        await self._sync_coordinator.run_maintenance(
            _remove, timeout_=_MAINTENANCE_TIMEOUT
        )

    @lru_cache(maxsize=128)
    def get_profiles_for_library_provider(self, namespace: str) -> Sequence[str]:
        """Find all profile names and their configs by provider account id.

        This is memoized to avoid repeated linear scans of profile lists for
        frequent webhook requests.

        Args:
            namespace (str): The provider namespace to search for.

        Returns:
            Sequence[str]: A sequence of profile names matching the namespace.

        Raises:
            KeyError: If no profile matches the given account id
        """
        profiles: list[str] = []
        for profile_name, bridge_client in self.bridge_clients.items():
            if bridge_client is None:
                continue
            if namespace == bridge_client.library_provider.NAMESPACE:
                profiles.append(profile_name)

        if not profiles:
            raise ProfileNotFoundError(
                f"Profile for library provider namespace '{namespace}' not found"
            )

        return profiles

    async def _sync_profile_once(
        self,
        profile_name: str,
        poll: bool,
        library_keys: Sequence[str] | None,
    ) -> None:
        """Execute a one-off profile sync when no profile scheduler is active."""
        bridge_client = self.bridge_clients.get(profile_name)
        if bridge_client is None:
            raise ProfileNotFoundError(f"Profile '{profile_name}' not found")

        await self._sync_coordinator.acquire_profile_slot(profile_name)
        try:
            await bridge_client.sync(poll=poll, library_keys=library_keys)
        finally:
            self._sync_coordinator.release_profile_slot(profile_name)

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop()
