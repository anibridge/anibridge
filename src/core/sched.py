"""Scheduler Module."""

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from src import log
from src.config.settings import AniBridgeConfig, ScanMode
from src.core.animap import AnimapClient
from src.core.bridge import BridgeClient
from src.exceptions import ProfileNotFoundError, SchedulerUnavailableError
from src.utils.cache import lru_cache

__all__ = ["SchedulerClient"]


class ProfileScheduler:
    """Individual profile scheduler for managing sync operations.

    Handles the scheduling logic for a single profile, including periodic
    sync, polling mode, and single-run mode.
    """

    DEFAULT_MAX_PENDING_WAITERS = 256

    def __init__(
        self,
        profile_name: str,
        bridge_client: BridgeClient,
        scan_interval: int,
        scan_modes: list[ScanMode],
        poll_interval: int = 30,
        max_pending_waiters: int = DEFAULT_MAX_PENDING_WAITERS,
        stop_event: asyncio.Event | None = None,
    ):
        """Initialize a profile scheduler.

        Args:
            profile_name: Name of the profile
            bridge_client: Bridge client for this profile
            scan_interval: Sync interval in seconds
            scan_modes: List of sync modes enabled for this profile
            poll_interval: Polling interval in seconds
            max_pending_waiters: Maximum number of pending request waiters
            stop_event: Event to signal shutdown
        """
        self.profile_name = profile_name
        self.bridge_client = bridge_client
        self.scan_interval = scan_interval
        self.scan_modes = scan_modes
        self.poll_interval = poll_interval
        self.max_pending_waiters = max_pending_waiters
        self.stop_event = stop_event or asyncio.Event()

        self._running = False
        self._sync_lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()  # Prevents early GC
        self._worker_task: asyncio.Task | None = None

        self._pending_lock = asyncio.Lock()
        self._pending_poll = False
        self._pending_library_keys: set[str] | None = set()
        self._pending_sources: set[str] = set()
        self._pending_waiters: list[asyncio.Future[None]] = []
        self._pending_event = asyncio.Event()

        self._sync_requests_total = 0
        self._sync_requests_coalesced = 0
        self._sync_requests_rejected = 0
        self._last_sync_sources: tuple[str, ...] = tuple()

    async def _execute_sync(
        self, poll: bool = False, library_keys: Sequence[str] | None = None
    ) -> None:
        """Execute a single synchronization cycle with error handling."""
        async with self._sync_lock:
            try:
                self._current_task = asyncio.create_task(
                    self.bridge_client.sync(poll=poll, library_keys=library_keys)
                )
                await self._current_task
            except asyncio.CancelledError:
                if self._current_task and not self._current_task.done():
                    log.info(
                        "[%s] Cancelling sync task...",
                        self.profile_name,
                    )
                    self._current_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._current_task
                raise
            except Exception:
                log.error("[%s] Sync error", self.profile_name)
                log.exception(
                    "[%s] Sync error details",
                    self.profile_name,
                )
            finally:
                self._current_task = None

    async def _enqueue_sync(
        self,
        poll: bool = False,
        library_keys: Sequence[str] | None = None,
        source: str = "manual",
    ) -> asyncio.Future[None]:
        """Queue a sync request for the profile worker."""
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        normalized_keys = None if library_keys is None else set(library_keys)
        async with self._pending_lock:
            if len(self._pending_waiters) >= self.max_pending_waiters:
                self._sync_requests_rejected += 1
                raise SchedulerUnavailableError(
                    f"Profile '{self.profile_name}' sync queue is full"
                )

            self._sync_requests_total += 1
            if self._pending_waiters:
                self._sync_requests_coalesced += 1

            if not self._pending_waiters:
                self._pending_poll = poll
            else:
                self._pending_poll = self._pending_poll and poll

            if normalized_keys is None:
                self._pending_library_keys = None
            elif self._pending_library_keys is not None:
                self._pending_library_keys.update(normalized_keys)

            self._pending_sources.add(source)
            self._pending_waiters.append(future)
            self._pending_event.set()

        return future

    async def _drain_pending(
        self,
    ) -> tuple[bool, list[str] | None, list[asyncio.Future[None]], tuple[str, ...]]:
        """Take a snapshot of pending sync requests and clear state."""
        async with self._pending_lock:
            poll = self._pending_poll
            pending_keys = self._pending_library_keys
            pending_sources = tuple(sorted(self._pending_sources))
            waiters = self._pending_waiters

            self._pending_poll = False
            self._pending_library_keys = set()
            self._pending_sources = set()
            self._pending_waiters = []
            self._pending_event.clear()

        library_keys = None if pending_keys is None else list(pending_keys)
        return poll, library_keys, waiters, pending_sources

    async def _fail_pending(self, exc: Exception | BaseException) -> None:
        """Fail all pending sync futures with an exception."""
        async with self._pending_lock:
            waiters = self._pending_waiters
            self._pending_waiters = []
            self._pending_poll = False
            self._pending_library_keys = set()
            self._pending_sources = set()
            self._pending_event.clear()

        for waiter in waiters:
            if not waiter.done():
                waiter.set_exception(exc)

    async def _sync_worker(self) -> None:
        """Process sync requests sequentially for this profile."""
        try:
            while self._running and not self.stop_event.is_set():
                wait_task = asyncio.create_task(self.stop_event.wait())
                event_task = asyncio.create_task(self._pending_event.wait())
                done, pending = await asyncio.wait(
                    {wait_task, event_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

                if wait_task in done and self.stop_event.is_set():
                    break
                if event_task not in done:
                    continue

                poll, library_keys, waiters, sources = await self._drain_pending()
                self._last_sync_sources = sources

                log.debug(
                    "[%s] Running sync: waiters=%s, poll=%s, keys=%s, sources=%s",
                    self.profile_name,
                    len(waiters),
                    poll,
                    "all" if library_keys is None else len(library_keys),
                    ",".join(sources) if sources else "unknown",
                )

                try:
                    await self._execute_sync(poll=poll, library_keys=library_keys)
                except asyncio.CancelledError:
                    for waiter in waiters:
                        if not waiter.done():
                            waiter.cancel()
                    raise
                except Exception as exc:
                    for waiter in waiters:
                        if not waiter.done():
                            waiter.set_exception(exc)
                else:
                    for waiter in waiters:
                        if not waiter.done():
                            waiter.set_result(None)
        except asyncio.CancelledError:
            raise
        finally:
            await self._fail_pending(asyncio.CancelledError())

    async def sync(
        self,
        poll: bool = False,
        library_keys: Sequence[str] | None = None,
        source: str = "manual",
    ) -> None:
        """Execute a single synchronization cycle with error handling.

        Args:
            poll (bool): Flag to enable polling-based sync.
            library_keys (Sequence[str] | None): Sequence of library media keys to
                restrict the sync scope.
            source (str): Origin of the sync request for telemetry.
        """
        if not self._running or self._worker_task is None:
            self._last_sync_sources = (source,)
            await self._execute_sync(poll=poll, library_keys=library_keys)
            return

        future = await self._enqueue_sync(
            poll=poll,
            library_keys=library_keys,
            source=source,
        )
        await future

    async def get_runtime_metrics(self) -> dict[str, Any]:
        """Return runtime mailbox and queue metrics for this profile."""
        async with self._pending_lock:
            return {
                "pending_waiters": len(self._pending_waiters),
                "pending_poll": self._pending_poll,
                "pending_scope": "all"
                if self._pending_library_keys is None
                else len(self._pending_library_keys),
                "pending_sources": sorted(self._pending_sources),
                "requests_total": self._sync_requests_total,
                "requests_coalesced": self._sync_requests_coalesced,
                "requests_rejected": self._sync_requests_rejected,
                "max_pending_waiters": self.max_pending_waiters,
                "last_sync_sources": list(self._last_sync_sources),
            }

    async def start(self) -> None:
        """Start the profile scheduler."""
        if self._running:
            return

        self._running = True
        self._worker_task = asyncio.create_task(self._sync_worker())

        if ScanMode.PERIODIC in self.scan_modes:
            self._spawn_loop(
                name="periodic",
                interval=self.scan_interval,
                poll=False,
            )

        if ScanMode.POLL in self.scan_modes:
            self._spawn_loop(
                name="poll",
                interval=self.poll_interval,
                poll=True,
            )

    async def stop(self) -> None:
        """Stop the profile scheduler."""
        self._running = False
        self.stop_event.set()

        for task in list(self._tasks):
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        worker_task = self._worker_task
        if worker_task and not worker_task.done():
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        self._worker_task = None

        current_task = self._current_task
        if current_task and not current_task.done():
            current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await current_task

    def _spawn_loop(self, *, name: str, interval: int, poll: bool) -> None:
        """Create and track a looping sync task."""
        log.debug(
            "[%s] Starting %s sync every %ss",
            self.profile_name,
            name,
            interval,
        )
        task = asyncio.create_task(
            self._run_loop(name=name, interval=interval, poll=poll)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_loop(self, *, name: str, interval: int, poll: bool) -> None:
        """Handle periodic or polling synchronization loops."""
        while self._running and not self.stop_event.is_set():
            try:
                await self.sync(poll=poll, source=f"loop:{name}")

                if not poll:
                    next_sync = datetime.now(UTC) + timedelta(seconds=interval)
                    log.info(
                        "[%s] Next %s sync scheduled for: %s",
                        self.profile_name,
                        name,
                        next_sync.astimezone(),
                    )

                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.stop_event.wait(), interval)
            except asyncio.CancelledError:
                log.debug(
                    "[%s] %s sync cancelled",
                    self.profile_name,
                    name,
                )
                break
            except Exception:
                log.error(
                    "[%s] %s sync error",
                    self.profile_name,
                    name,
                )
                log.exception(
                    "[%s] %s sync error details",
                    self.profile_name,
                    name,
                )
                await asyncio.sleep(10)


class SchedulerClient:
    """Application scheduler that manages all profiles and global tasks.

    Coordinates multiple profile schedulers and handles shared resources like
    the daily database sync. Provides centralized management and graceful shutdown.
    """

    def __init__(self, global_config: AniBridgeConfig):
        """Initialize the application scheduler.

        Args:
            global_config (AniBridgeConfig): Global application configuration.
        """
        self.global_config = global_config
        self.shared_animap_client = AnimapClient(
            global_config.data_path, global_config.mappings_url
        )
        self.bridge_clients: dict[str, BridgeClient] = {}
        self.profile_schedulers: dict[str, ProfileScheduler] = {}
        self.stop_event = asyncio.Event()
        self._running = False
        self._daily_sync_task: asyncio.Task | None = None

    def request_shutdown(self) -> None:
        """Request application shutdown from external callers."""
        if not self.stop_event.is_set():
            self.stop_event.set()

    @property
    def is_running(self) -> bool:
        """Return whether the scheduler main loop is currently running."""
        return self._running

    def get_next_database_sync_at(self) -> datetime | None:
        """Return the next scheduled database sync time in UTC."""
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

        for profile_name, profile_config in self.global_config.profiles.items():
            log.info("[%s] Setting up bridge client", profile_name)

            try:
                bridge_client = BridgeClient(
                    profile_name=profile_name,
                    profile_config=profile_config,
                    global_config=self.global_config,
                    shared_animap_client=self.shared_animap_client,
                )

                await bridge_client.initialize()
                self.bridge_clients[profile_name] = bridge_client

                log.info("[%s] Bridge client ready", profile_name)
            except Exception:
                log.error("[%s] Bridge client setup failed", profile_name)
                log.exception(
                    "[%s] Bridge setup error details",
                    profile_name,
                )

        log.info(
            "Application scheduler initialized with %s profile(s)",
            len(self.bridge_clients),
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
            profile_config = self.global_config.get_profile(profile_name)

            log.info(
                "[%s] Starting scheduler: interval=%ss, modes=%s, full_scan=%s, "
                "destructive=%s",
                profile_name,
                profile_config.scan_interval,
                profile_config.scan_modes,
                "enabled" if profile_config.full_scan else "disabled",
                "enabled" if profile_config.destructive_sync else "disabled",
            )

            scheduler = ProfileScheduler(
                profile_name=profile_name,
                bridge_client=bridge_client,
                scan_interval=profile_config.scan_interval,
                scan_modes=profile_config.scan_modes,
                poll_interval=30,
                stop_event=self.stop_event,
            )

            self.profile_schedulers[profile_name] = scheduler
            await scheduler.start()

            if profile_config.scan_modes:
                next_sync_time = "in progress"
                if (
                    ScanMode.PERIODIC in profile_config.scan_modes
                    and profile_config.scan_interval > 0
                ):
                    next_sync = datetime.now(UTC).astimezone()
                    next_sync_time = "at {}".format(
                        next_sync.strftime("%Y-%m-%d %H:%M:%S")
                    )

                log.info(
                    "[%s] Scheduler started, next sync: %s",
                    profile_name,
                    next_sync_time,
                )

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

    async def trigger_sync(
        self,
        profile_name: str | None = None,
        poll: bool = False,
        library_keys: Sequence[str] | None = None,
        source: str = "manual",
    ) -> None:
        """Manually trigger a sync for one or all profiles.

        Args:
            profile_name (str | None): Specific profile to sync, or None for all.
            poll (bool): Whether to use polling mode for the sync.
            library_keys (Sequence[str] | None): Optional list of library media keys to.
                restrict the sync scope for each profile.
            source (str): Origin of the trigger request.

        Raises:
            KeyError: If the specified profile doesn't exist
        """
        if profile_name is not None:
            if profile_name not in self.bridge_clients:
                raise ProfileNotFoundError(f"Profile '{profile_name}' not found")

            log.info(
                "[%s] Triggering sync (poll=%s, source=%s)",
                profile_name,
                poll,
                source,
            )
            scheduler = self.profile_schedulers[profile_name]
            await scheduler.sync(poll=poll, library_keys=library_keys, source=source)
        else:
            log.info(
                "Triggering sync for all profiles (poll=%s, source=%s)",
                poll,
                source,
            )
            sync_tasks = []
            for name, scheduler in self.profile_schedulers.items():
                log.info("[%s] Triggering sync", name)
                sync_tasks.append(
                    scheduler.sync(
                        poll=poll,
                        library_keys=library_keys,
                        source=source,
                    )
                )

            if sync_tasks:
                await asyncio.gather(*sync_tasks, return_exceptions=True)

    async def get_status(self) -> dict[str, dict[str, Any]]:
        """Get the status of all profiles.

        Returns:
            dict[str, dict[str, Any]]: A dictionary containing the profile info.
        """
        status = {}

        for profile_name in self.bridge_clients:
            profile_config = self.global_config.get_profile(profile_name)
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
                    "library_namespace": library_namespace,
                    "list_namespace": list_namespace,
                    "library_user": library_user_title,
                    "list_user": list_user_title,
                    "scan_interval": profile_config.scan_interval,
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
                        bridge_client.current_sync.model_dump(mode="json")
                        if bridge_client and bridge_client.current_sync is not None
                        else None
                    ),
                    "scheduler": await scheduler.get_runtime_metrics()
                    if scheduler is not None
                    and hasattr(scheduler, "get_runtime_metrics")
                    else None,
                },
            }

        return status

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

                sleep_duration = (next_sync_time - now).total_seconds()

                log.info(
                    "Next database sync scheduled for: %s (in %.1f hours)",
                    next_sync_time.astimezone(),
                    sleep_duration / 3600,
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
                    await self.shared_animap_client.sync_db()
                    log.success("Daily database sync completed")

                    log.info("Reinitializing all list providers")
                    for bridge_client in self.bridge_clients.values():
                        await bridge_client.list_provider.clear_cache()
                        await bridge_client._backup_list()
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

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop()
