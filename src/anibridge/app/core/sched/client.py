"""Application scheduler."""

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import msgspec
from anibridge.utils.cache import lru_cache

from anibridge.app.config.settings import AnibridgeConfig, ScanMode
from anibridge.app.core.animap import AnimapClient
from anibridge.app.core.bridge import BridgeClient
from anibridge.app.core.sync import (
    RecordUndoRequest,
    SyncRequest,
    SyncTrigger,
    dedupe_refs,
)
from anibridge.app.exceptions import ProfileNotFoundError, SchedulerUnavailableError
from anibridge.app.logging import get_logger
from anibridge.app.utils.cron import (
    CronStr,
    get_next_interval_seconds,
    get_next_run_datetime,
)
from anibridge.app.utils.human import human_duration
from anibridge.app.utils.memory import release_memory

__all__ = ["SchedulerClient"]

log = get_logger(__name__)

_MAINTENANCE_TIMEOUT: float = 3600


class _QueuedSync(msgspec.Struct):
    """One queued profile sync request."""

    profile_name: str
    request: SyncRequest
    source: str
    future: asyncio.Future[None]


class SchedulerClient:
    """Application scheduler backed by one global sync queue.

    Profile periodic/poll producers enqueue work into the same queue used by manual
    and webhook callers. The worker executes one profile sync at a time, coalescing
    pending requests for the same profile before execution.
    """

    DEFAULT_MAX_PENDING_SYNCS = 512

    def __init__(self, global_config: AnibridgeConfig):
        """Initialize the application scheduler."""
        self.global_config = global_config
        self.shared_animap_client = AnimapClient(
            global_config.data_path, global_config.mappings_url
        )
        self.bridge_clients: dict[str, BridgeClient] = {}
        self.failed_profile_errors: dict[str, str] = {}
        self.stop_event = asyncio.Event()

        self._running = False
        self._daily_sync_task: asyncio.Task | None = None
        self._sync_worker_task: asyncio.Task | None = None
        self._producer_tasks: set[asyncio.Task] = set()
        self._sync_queue: asyncio.Queue[_QueuedSync] = asyncio.Queue(
            maxsize=self.DEFAULT_MAX_PENDING_SYNCS
        )
        self._maintenance_lock = asyncio.Lock()

        self._active_profile: str | None = None
        self._sync_requests_total = 0
        self._sync_requests_coalesced = 0
        self._sync_requests_rejected = 0
        self._pending_sync_counts: dict[str, int] = {}
        self._last_sync_sources: dict[str, tuple[str, ...]] = {}

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
        return self._get_next_1am_utc(datetime.now(UTC))

    async def initialize(self) -> None:
        """Initialize the mapping database and all configured bridge clients."""
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

        tasks = [
            asyncio.create_task(init_bridge(profile_name, profile_config))
            for profile_name, profile_config in self.global_config.profiles.items()
        ]
        if tasks:
            await asyncio.gather(*tasks)

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
            log.exception("[%s] Bridge setup error details", profile_name)
            if bridge_client is not None:
                with contextlib.suppress(Exception):
                    await bridge_client.close()
            raise

        log.info("[%s] Bridge client initialized", profile_name)
        return bridge_client

    async def start(self) -> None:
        """Start the single sync worker plus profile trigger producers."""
        if self._running:
            return

        if self.stop_event.is_set():
            self.stop_event = asyncio.Event()

        self._running = True
        log.info("Starting application scheduler")

        self._sync_worker_task = asyncio.create_task(self._sync_worker())
        self._daily_sync_task = asyncio.create_task(self._daily_db_sync_loop())

        for profile_name in self.bridge_clients:
            self._start_profile_producers(profile_name)

        if self.bridge_clients and all(
            not self.global_config.get_profile(name).scan_modes
            for name in self.bridge_clients
        ):
            log.info(
                "None of the profiles have scan modes enabled; scheduler will remain "
                "idle until manually triggered",
            )

        log.info(
            "Application scheduler started with %s bridge client(s)",
            len(self.bridge_clients),
        )

    def _start_profile_producers(self, profile_name: str) -> None:
        """Start periodic/poll producer loops for one profile."""
        profile_config = self.global_config.get_profile(profile_name)
        log.info(
            "[%s] Scheduling profile: poll_interval=%s, scan_interval=%s, "
            "modes=%s, full_scan=%s, destructive=%s",
            profile_name,
            human_duration(profile_config.poll_interval)
            if isinstance(profile_config.poll_interval, int)
            else profile_config.poll_interval,
            human_duration(profile_config.scan_interval)
            if isinstance(profile_config.scan_interval, int)
            else profile_config.scan_interval,
            profile_config.scan_modes,
            "enabled" if profile_config.full_scan else "disabled",
            "enabled" if profile_config.destructive_sync else "disabled",
        )

        if ScanMode.PERIODIC in profile_config.scan_modes:
            self._spawn_producer(
                profile_name=profile_name,
                name="periodic",
                interval=profile_config.scan_interval,
                request=SyncRequest(trigger=SyncTrigger.PERIODIC),
            )
        if ScanMode.POLL in profile_config.scan_modes:
            self._spawn_producer(
                profile_name=profile_name,
                name="poll",
                interval=profile_config.poll_interval,
                request=SyncRequest(trigger=SyncTrigger.POLL),
            )

    def _spawn_producer(
        self,
        *,
        profile_name: str,
        name: str,
        interval: int | CronStr,
        request: SyncRequest,
    ) -> None:
        """Spawn one profile trigger producer."""
        task = asyncio.create_task(
            self._producer_loop(
                profile_name=profile_name,
                name=name,
                interval=interval,
                request=request,
            ),
            name=f"profile:{profile_name}:{name}",
        )
        self._producer_tasks.add(task)
        task.add_done_callback(self._producer_tasks.discard)

    async def _producer_loop(
        self,
        *,
        profile_name: str,
        name: str,
        interval: int | CronStr,
        request: SyncRequest,
    ) -> None:
        """Periodically enqueue profile sync requests."""
        is_cron = isinstance(interval, str)
        first = True
        while self._running and not self.stop_event.is_set():
            try:
                wait_time = get_next_interval_seconds(interval, datetime.now())
                if is_cron or not first:
                    log.info(
                        "[%s] Next %s sync scheduled for %s (in %s)",
                        profile_name,
                        name,
                        get_next_run_datetime(interval),
                        human_duration(wait_time),
                    )
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.stop_event.wait(), wait_time)
                    if not self._running or self.stop_event.is_set():
                        break

                await self.trigger_profile_sync(
                    profile_name,
                    request=request,
                    source=f"loop:{name}",
                )
                first = False
            except asyncio.CancelledError:
                break
            except Exception:
                log.error("[%s] %s producer error", profile_name, name)
                log.exception("[%s] %s producer error details", profile_name, name)
                await asyncio.sleep(10)

    async def stop(self) -> None:
        """Stop producers, worker, bridges, and shared resources."""
        if not self._running:
            return

        self._running = False
        log.info("Stopping application scheduler")
        self.stop_event.set()

        for task in tuple(self._producer_tasks):
            task.cancel()
        if self._producer_tasks:
            await asyncio.gather(*self._producer_tasks, return_exceptions=True)
        self._producer_tasks.clear()

        if self._daily_sync_task and not self._daily_sync_task.done():
            self._daily_sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._daily_sync_task

        worker = self._sync_worker_task
        if worker and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._sync_worker_task = None
        self._fail_queued(asyncio.CancelledError())

        close_tasks = [
            bridge_client.close() for bridge_client in self.bridge_clients.values()
        ]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)

        await self.shared_animap_client.close()

        self.bridge_clients.clear()
        self.get_profiles_for_source_provider.cache_clear()
        log.info("Application scheduler stopped")

    async def wait_for_completion(self) -> None:
        """Wait until shutdown is requested."""
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
        request: SyncRequest | None = None,
        source: str = "manual",
    ) -> None:
        """Trigger a sync for a single profile."""
        if profile_name not in self.bridge_clients:
            raise ProfileNotFoundError(f"Profile '{profile_name}' not found")

        request = request or SyncRequest()
        log.info(
            "[%s] Triggering sync (trigger=%s, refs=%s, source=%s)",
            profile_name,
            request.trigger.value,
            len(request.source_refs) if request.source_refs is not None else "all",
            source,
        )

        if not self._running or self._sync_worker_task is None:
            await self._sync_profile_once(profile_name=profile_name, request=request)
            self._last_sync_sources[profile_name] = (source,)
            return

        future = self._enqueue_sync(
            profile_name=profile_name,
            request=request,
            source=source,
        )
        await future

    async def trigger_all_profiles_sync(
        self,
        request: SyncRequest | None = None,
        source: str = "manual",
    ) -> None:
        """Trigger a sync for all initialized profiles."""
        request = request or SyncRequest()
        profile_names = tuple(self.bridge_clients)
        if not profile_names:
            log.warning("No profiles available to sync")
            return

        results = await asyncio.gather(
            *(
                self.trigger_profile_sync(
                    profile_name=name,
                    request=request,
                    source=source,
                )
                for name in profile_names
            ),
            return_exceptions=True,
        )
        exceptions = [result for result in results if isinstance(result, Exception)]
        if exceptions:
            raise ExceptionGroup(
                "One or more profile sync triggers failed",
                exceptions,
            )

    def _enqueue_sync(
        self,
        *,
        profile_name: str,
        request: SyncRequest,
        source: str,
    ) -> asyncio.Future[None]:
        """Queue one profile sync request."""
        if self._sync_queue.full():
            self._sync_requests_rejected += 1
            raise SchedulerUnavailableError("Scheduler sync queue is full")

        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sync_requests_total += 1
        self._pending_sync_counts[profile_name] = (
            self._pending_sync_counts.get(profile_name, 0) + 1
        )
        self._sync_queue.put_nowait(
            _QueuedSync(
                profile_name=profile_name,
                request=request,
                source=source,
                future=future,
            )
        )
        return future

    async def _sync_worker(self) -> None:
        """Run queued sync work serially."""
        try:
            while self._running and not self.stop_event.is_set():
                wait_task = asyncio.create_task(self.stop_event.wait())
                queue_task = asyncio.create_task(self._sync_queue.get())
                done, pending = await asyncio.wait(
                    {wait_task, queue_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

                if wait_task in done and self.stop_event.is_set():
                    if queue_task in done:
                        queued = queue_task.result()
                        self._remove_pending_count(queued.profile_name)
                        if not queued.future.done():
                            queued.future.cancel()
                    break
                if queue_task not in done:
                    continue

                queued = queue_task.result()
                request, waiters, sources = self._coalesce_profile_requests(queued)
                self._last_sync_sources[queued.profile_name] = sources
                try:
                    async with self._maintenance_lock:
                        self._active_profile = queued.profile_name
                        await self._sync_profile_once(
                            profile_name=queued.profile_name,
                            request=request,
                        )
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
                finally:
                    self._active_profile = None
        except asyncio.CancelledError:
            raise
        finally:
            self._fail_queued(asyncio.CancelledError())

    def _coalesce_profile_requests(
        self,
        first: _QueuedSync,
    ) -> tuple[SyncRequest, list[asyncio.Future[None]], tuple[str, ...]]:
        """Drain currently pending requests for the same profile and merge them."""
        matching = [first]
        deferred: list[_QueuedSync] = []
        while True:
            try:
                queued = self._sync_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if queued.profile_name == first.profile_name:
                matching.append(queued)
            else:
                deferred.append(queued)

        for queued in deferred:
            self._sync_queue.put_nowait(queued)

        # Counts model logical waiters, so only requests consumed for this sync leave
        # the pending set; deferred profiles stay queued even though we peeked at them.
        for queued in matching:
            self._remove_pending_count(queued.profile_name)

        self._sync_requests_coalesced += max(0, len(matching) - 1)
        request = self._coalesced_request([item.request for item in matching])
        waiters = [item.future for item in matching]
        sources = tuple(sorted({item.source for item in matching}))
        return request, waiters, sources

    @staticmethod
    def _coalesced_request(requests: Sequence[SyncRequest]) -> SyncRequest:
        """Merge queued requests without losing scan coverage."""
        trigger_priority = {
            SyncTrigger.MANUAL: 5,
            SyncTrigger.POLL: 4,
            SyncTrigger.WEBHOOK: 3,
            SyncTrigger.PERIODIC: 2,
        }
        trigger = max(requests, key=lambda item: trigger_priority[item.trigger]).trigger
        refs = []
        record_undos: list[RecordUndoRequest] = []
        full_source_scan = False
        full_scan_on_poll_fallback = False
        for request in requests:
            record_undos.extend(request.record_undos)
            full_scan_on_poll_fallback = (
                full_scan_on_poll_fallback or request.full_scan_on_poll_fallback
            )
            if request.source_refs is None:
                # A full manual/periodic scan covers any targeted work already queued.
                if request.trigger in {SyncTrigger.MANUAL, SyncTrigger.PERIODIC}:
                    full_source_scan = True
                elif request.trigger == SyncTrigger.POLL:
                    full_scan_on_poll_fallback = True
                continue
            refs.extend(request.source_refs)

        if full_scan_on_poll_fallback and trigger != SyncTrigger.POLL:
            full_source_scan = True

        source_refs = (
            None
            if full_source_scan or (not refs and trigger == SyncTrigger.POLL)
            else dedupe_refs(refs)
        )
        return SyncRequest(
            trigger=trigger,
            source_refs=source_refs,
            full_scan_on_poll_fallback=(
                full_scan_on_poll_fallback and trigger == SyncTrigger.POLL
            ),
            record_undos=tuple(record_undos),
        )

    def _fail_queued(self, exc: BaseException) -> None:
        """Fail all pending queued sync requests."""
        while True:
            try:
                queued = self._sync_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._remove_pending_count(queued.profile_name)
            if not queued.future.done():
                queued.future.set_exception(exc)

    def _remove_pending_count(self, profile_name: str) -> None:
        """Update queue counters when a request leaves the pending queue."""
        remaining = self._pending_sync_counts.get(profile_name, 0) - 1
        if remaining > 0:
            self._pending_sync_counts[profile_name] = remaining
        else:
            self._pending_sync_counts.pop(profile_name, None)

    async def get_status(self) -> dict[str, Any]:
        """Get status for all configured profiles."""
        queued_counts = dict(self._pending_sync_counts)

        status = {}
        for profile_name, profile_config in self.global_config.profiles.items():
            bridge_client = self.bridge_clients.get(profile_name)

            source_namespace: str | None = profile_config.source_provider
            target_namespace: str | None = profile_config.target_provider
            source_account_title: str | None = None
            target_account_title: str | None = None

            if bridge_client is not None:
                source_namespace = bridge_client.source_provider.NAMESPACE
                target_namespace = bridge_client.target_provider.NAMESPACE

                source_account = bridge_client.source_provider.account()
                if source_account is not None:
                    source_account_title = source_account.title

                target_account = bridge_client.target_provider.account()
                if target_account is not None:
                    target_account_title = target_account.title

            status[profile_name] = {
                "config": {
                    "source_namespace": source_namespace,
                    "target_namespace": target_namespace,
                    "source_account": source_account_title,
                    "target_account": target_account_title,
                    "scan_interval": profile_config.scan_interval,
                    "poll_interval": profile_config.poll_interval,
                    "scan_modes": [m.value for m in profile_config.scan_modes],
                    "full_scan": profile_config.full_scan,
                    "destructive_sync": profile_config.destructive_sync,
                },
                "status": {
                    "running": self._running and bridge_client is not None,
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
                    "scheduler": {
                        "pending_waiters": queued_counts.get(profile_name, 0),
                        "last_sync_sources": list(
                            self._last_sync_sources.get(profile_name, ())
                        ),
                        "running": self._running,
                        "sync_active": self._active_profile == profile_name,
                    },
                },
            }

        return status

    async def get_runtime_metrics(self) -> dict[str, Any]:
        """Return scheduler-level runtime metrics."""
        return {
            "running": self._running,
            "profile_count": len(self.global_config.profiles),
            "bridge_count": len(self.bridge_clients),
            "queue_depth": self._sync_queue.qsize(),
            "requests_total": self._sync_requests_total,
            "requests_coalesced": self._sync_requests_coalesced,
            "requests_rejected": self._sync_requests_rejected,
            "active_profile": self._active_profile,
            "producer_count": len(self._producer_tasks),
            "daily_sync_active": self._daily_sync_task is not None
            and not self._daily_sync_task.done(),
        }

    async def trigger_database_sync(self, source: str = "manual:database") -> None:
        """Run mapping DB sync and target-provider backups."""
        log.info("Starting database sync (source=%s)", source)
        try:
            async with asyncio.timeout(_MAINTENANCE_TIMEOUT):
                async with self._maintenance_lock:
                    await self.shared_animap_client.sync_db()
                    log.success("Database sync completed (source=%s)", source)

                    backup_tasks = [
                        bridge_client._backup_target()
                        for bridge_client in self.bridge_clients.values()
                    ]
                    if backup_tasks:
                        results = await asyncio.gather(
                            *backup_tasks,
                            return_exceptions=True,
                        )
                        exceptions = [
                            result
                            for result in results
                            if isinstance(result, Exception)
                        ]
                        if exceptions:
                            raise ExceptionGroup(
                                "One or more daily profile backups failed",
                                exceptions,
                            )
        except TimeoutError:
            log.error(
                "Database sync timed out after %d seconds (source=%s)",
                _MAINTENANCE_TIMEOUT,
                source,
            )
            raise
        release_memory()

    def _get_next_1am_utc(self, now: datetime) -> datetime:
        """Calculate the next 1:00 AM UTC."""
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
                await self.trigger_database_sync(source="loop:daily_db")
            except asyncio.CancelledError:
                break
            except Exception:
                log.error("Daily database sync error")
                log.exception("Daily database sync loop error details")
                await asyncio.sleep(3600)

        log.info("Daily database sync scheduler stopped")

    async def reinitialize_profile(self, profile_name: str) -> None:
        """Rebuild one profile bridge and restart its producers if running."""
        if profile_name not in self.global_config.profiles:
            raise ProfileNotFoundError(f"Profile '{profile_name}' not found")

        async with asyncio.timeout(_MAINTENANCE_TIMEOUT):
            async with self._maintenance_lock:
                log.info("[%s] Reinitializing profile", profile_name)
                self._cancel_profile_producers(profile_name)

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
                    self._start_profile_producers(profile_name)
                self.get_profiles_for_source_provider.cache_clear()
                log.success("[%s] Profile reinitialized successfully", profile_name)

    async def remove_profile(self, profile_name: str) -> None:
        """Remove one profile bridge from the runtime scheduler."""
        async with asyncio.timeout(_MAINTENANCE_TIMEOUT):
            async with self._maintenance_lock:
                log.info("[%s] Removing profile from runtime scheduler", profile_name)
                self._cancel_profile_producers(profile_name)
                bridge_client = self.bridge_clients.pop(profile_name, None)
                if bridge_client is not None:
                    await bridge_client.close()
                self.failed_profile_errors.pop(profile_name, None)
                self.get_profiles_for_source_provider.cache_clear()
                log.success("[%s] Profile removed from runtime scheduler", profile_name)

    def _cancel_profile_producers(self, profile_name: str) -> None:
        """Cancel producer tasks for a profile by name convention."""
        for task in tuple(self._producer_tasks):
            if task.get_name().startswith(f"profile:{profile_name}:"):
                task.cancel()

    @lru_cache(maxsize=128)
    def get_profiles_for_source_provider(self, namespace: str) -> Sequence[str]:
        """Find profile names that use a given source provider namespace."""
        profiles = [
            profile_name
            for profile_name, bridge_client in self.bridge_clients.items()
            if bridge_client is not None
            and namespace == bridge_client.source_provider.NAMESPACE
        ]
        if not profiles:
            raise ProfileNotFoundError(
                f"Profile for source provider namespace '{namespace}' not found"
            )
        return profiles

    async def _sync_profile_once(
        self,
        profile_name: str,
        request: SyncRequest,
    ) -> None:
        """Execute one profile sync."""
        bridge_client = self.bridge_clients.get(profile_name)
        if bridge_client is None:
            raise ProfileNotFoundError(f"Profile '{profile_name}' not found")
        await bridge_client.sync(request=request)

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop()
