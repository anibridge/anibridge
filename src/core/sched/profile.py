"""Profile-level scheduler actor."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from src import log
from src.config.settings import ScanMode
from src.core.bridge import BridgeClient
from src.exceptions import SchedulerUnavailableError


@dataclass(slots=True)
class _SyncRequest:
    poll: bool
    library_keys: Sequence[str] | None
    source: str
    future: asyncio.Future[None]


class ProfileScheduler:
    """Queue-backed single-profile sync scheduler."""

    DEFAULT_MAX_PENDING_WAITERS = 256

    def __init__(
        self,
        profile_name: str,
        bridge_client: BridgeClient,
        poll_interval: int,
        scan_interval: int,
        scan_modes: list[ScanMode],
        max_pending_waiters: int = DEFAULT_MAX_PENDING_WAITERS,
        before_sync: Callable[[str], Awaitable[None]] | None = None,
        after_sync: Callable[[str], Awaitable[None]] | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Initialize the queue-backed profile scheduler.

        Args:
            profile_name (str): The name of the profile this scheduler manages.
            bridge_client (BridgeClient): The bridge client used to perform syncs.
            poll_interval (int): The interval in seconds for poll scans.
            scan_interval (int): The interval in seconds for periodic scans.
            scan_modes (list[ScanMode]): The scan modes enabled for this profile.
            max_pending_waiters (int): The maximum number of sync requests to queue.
            before_sync (Callable[[str], Awaitable[None]] | None): Optional callback to
                run before each sync, receiving the profile name.
            after_sync (Callable[[str], Awaitable[None]] | None): Optional callback to
                run after each sync, receiving the profile name.
            stop_event (asyncio.Event | None): Optional event to signal the scheduler
                to stop, allowing external control over the scheduler lifecycle.
        """
        self.profile_name = profile_name
        self.bridge_client = bridge_client
        self.poll_interval = poll_interval
        self.scan_interval = scan_interval
        self.scan_modes = scan_modes
        self.max_pending_waiters = max_pending_waiters
        self._before_sync = before_sync
        self._after_sync = after_sync
        self.stop_event = stop_event or asyncio.Event()

        self._running = False
        self._current_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._worker_task: asyncio.Task | None = None
        self._request_queue: asyncio.Queue[_SyncRequest] = asyncio.Queue(
            maxsize=max_pending_waiters
        )

        self._sync_requests_total = 0
        self._sync_requests_coalesced = 0
        self._sync_requests_rejected = 0
        self._last_sync_sources: tuple[str, ...] = tuple()

    async def _execute_sync(
        self,
        poll: bool,
        library_keys: Sequence[str] | None,
    ) -> None:
        sync_slot_acquired = False
        try:
            if self._before_sync is not None:
                await self._before_sync(self.profile_name)
                sync_slot_acquired = True

            self._current_task = asyncio.create_task(
                self.bridge_client.sync(poll=poll, library_keys=library_keys)
            )
            await self._current_task
        except asyncio.CancelledError:
            if self._current_task and not self._current_task.done():
                self._current_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._current_task
            raise
        except Exception:
            log.error("[%s] Sync error", self.profile_name)
            log.exception("[%s] Sync error details", self.profile_name)
            raise
        finally:
            self._current_task = None
            if sync_slot_acquired and self._after_sync is not None:
                with contextlib.suppress(Exception):
                    await self._after_sync(self.profile_name)

    async def _enqueue_sync(
        self,
        poll: bool = False,
        library_keys: Sequence[str] | None = None,
        source: str = "manual",
    ) -> asyncio.Future[None]:
        """Enqueue a sync request and return a future."""
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        if self._request_queue.full():
            self._sync_requests_rejected += 1
            raise SchedulerUnavailableError(
                f"Profile '{self.profile_name}' sync queue is full"
            )

        self._sync_requests_total += 1
        self._request_queue.put_nowait(
            _SyncRequest(
                poll=poll,
                library_keys=library_keys,
                source=source,
                future=future,
            )
        )
        return future

    def _coalesce_requests(
        self,
        first: _SyncRequest,
    ) -> tuple[bool, list[str] | None, list[asyncio.Future[None]], tuple[str, ...]]:
        """Coalesce multiple pending sync requests into a single request."""
        requests = [first]
        while True:
            try:
                requests.append(self._request_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        self._sync_requests_coalesced += max(0, len(requests) - 1)
        poll = all(request.poll for request in requests)

        keys: set[str] | None = set()
        for request in requests:
            if request.library_keys is None:
                keys = None
                break
            if keys is not None:
                keys.update(request.library_keys)

        waiters = [request.future for request in requests]
        sources = tuple(sorted({request.source for request in requests}))
        library_keys = None if keys is None else list(keys)
        return poll, library_keys, waiters, sources

    def _fail_queued(self, exc: Exception | BaseException) -> None:
        """Fail all pending sync requests with the given exception."""
        while True:
            try:
                request = self._request_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not request.future.done():
                request.future.set_exception(exc)

    async def _sync_worker(self) -> None:
        """Main worker loop to process sync requests."""
        try:
            while self._running and not self.stop_event.is_set():
                wait_task = asyncio.create_task(self.stop_event.wait())
                queue_task = asyncio.create_task(self._request_queue.get())
                done, pending = await asyncio.wait(
                    {wait_task, queue_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

                if wait_task in done and self.stop_event.is_set():
                    break
                if queue_task not in done:
                    continue

                request = queue_task.result()
                poll, library_keys, waiters, sources = self._coalesce_requests(request)
                self._last_sync_sources = sources

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
            self._fail_queued(asyncio.CancelledError())

    async def sync(
        self,
        poll: bool = False,
        library_keys: Sequence[str] | None = None,
        source: str = "manual",
    ) -> None:
        """Queue or execute a profile sync request.

        Args:
            poll (bool): Whether to perform a poll sync.
            library_keys (Sequence[str] | None): Optional specific library keys to sync.
            source (str): The source of the sync request, used for logging and metrics.
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
        """Return profile scheduler queue and execution metrics."""
        return {
            "pending_waiters": self._request_queue.qsize(),
            "requests_total": self._sync_requests_total,
            "requests_coalesced": self._sync_requests_coalesced,
            "requests_rejected": self._sync_requests_rejected,
            "max_pending_waiters": self.max_pending_waiters,
            "last_sync_sources": list(self._last_sync_sources),
            "running": self._running,
            "sync_active": self._current_task is not None,
        }

    async def start(self) -> None:
        """Start the profile worker and optional loop producers."""
        if self._running:
            return

        self._running = True
        self._worker_task = asyncio.create_task(self._sync_worker())

        if ScanMode.PERIODIC in self.scan_modes:
            self._spawn_loop(name="periodic", interval=self.scan_interval, poll=False)

        if ScanMode.POLL in self.scan_modes:
            self._spawn_loop(name="poll", interval=self.poll_interval, poll=True)

    async def stop(self) -> None:
        """Stop the profile worker and cancel active tasks."""
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
        """Spawn a periodic loop task to trigger syncs at the given interval."""
        task = asyncio.create_task(
            self._run_loop(name=name, interval=interval, poll=poll)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_loop(self, *, name: str, interval: int, poll: bool) -> None:
        """Run a periodic loop to trigger syncs at the given interval."""
        while self._running and not self.stop_event.is_set():
            try:
                await self.sync(poll=poll, source=f"loop:{name}")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.stop_event.wait(), interval)
            except asyncio.CancelledError:
                break
            except Exception:
                log.error("[%s] %s sync error", self.profile_name, name)
                log.exception("[%s] %s sync error details", self.profile_name, name)
                await asyncio.sleep(10)
