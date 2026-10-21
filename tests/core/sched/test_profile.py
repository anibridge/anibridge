"""Tests for the profile scheduler actor."""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from typing import cast

import pytest

from anibridge.app.config.settings import ScanMode
from anibridge.app.core.bridge import BridgeClient
from anibridge.app.core.sched.profile import ProfileScheduler, _SyncRequest
from anibridge.app.exceptions import SchedulerUnavailableError


class _Bridge:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, list[str] | None]] = []
        self.behavior: str = "ok"

    async def sync(self, *, poll: bool, library_keys: list[str] | None) -> None:
        self.calls.append((poll, library_keys))
        if self.behavior == "error":
            raise RuntimeError("boom")
        if self.behavior == "cancel":
            raise asyncio.CancelledError


def _bridge_client(bridge: _Bridge | None = None) -> BridgeClient:
    return cast(BridgeClient, bridge or _Bridge())


def _get_bridge(scheduler: ProfileScheduler) -> _Bridge:
    return cast(_Bridge, scheduler.bridge_client)


@pytest.fixture
def bridge() -> _Bridge:
    return _Bridge()


@pytest.fixture
def profile_scheduler_factory() -> Callable[..., ProfileScheduler]:
    def _factory(
        *,
        bridge: _Bridge | None = None,
        poll_interval: int | str = 60,
        scan_interval: int | str = 120,
        scan_modes: list[ScanMode] | None = None,
        max_pending_waiters: int = ProfileScheduler.DEFAULT_MAX_PENDING_WAITERS,
    ) -> ProfileScheduler:
        return ProfileScheduler(
            profile_name="primary",
            bridge_client=_bridge_client(bridge),
            poll_interval=poll_interval,
            scan_interval=scan_interval,
            scan_modes=scan_modes or [],
            max_pending_waiters=max_pending_waiters,
        )

    return _factory


@pytest.fixture
def profile_scheduler(
    bridge: _Bridge,
    profile_scheduler_factory,
) -> ProfileScheduler:
    return profile_scheduler_factory(bridge=bridge)


@pytest.mark.asyncio
async def test_execute_sync_runs_callbacks(profile_scheduler: ProfileScheduler) -> None:
    events: list[str] = []

    async def _before(profile: str) -> None:
        events.append(f"before:{profile}")

    def _after(profile: str) -> None:
        events.append(f"after:{profile}")

    profile_scheduler._before_sync = _before
    profile_scheduler._after_sync = _after

    await profile_scheduler._execute_sync(poll=True, library_keys=["1"])

    assert events == ["before:primary", "after:primary"]
    assert _get_bridge(profile_scheduler).calls == [(True, ["1"])]


@pytest.mark.asyncio
async def test_enqueue_sync_rejects_when_queue_is_full(
    profile_scheduler: ProfileScheduler,
    profile_scheduler_factory,
) -> None:
    small = profile_scheduler_factory(max_pending_waiters=1)

    await small._enqueue_sync(source="first")
    with pytest.raises(SchedulerUnavailableError, match="queue is full"):
        await small._enqueue_sync(source="second")


@pytest.mark.asyncio
async def test_coalesce_requests_merges_sources_and_keys(
    profile_scheduler: ProfileScheduler,
) -> None:
    loop = asyncio.get_running_loop()
    first_future = await profile_scheduler._enqueue_sync(
        poll=True,
        library_keys=["1"],
        source="api",
    )
    second_future = loop.create_future()
    profile_scheduler._request_queue.put_nowait(
        _SyncRequest(
            poll=False,
            library_keys=["2", "3"],
            source="webhook",
            future=second_future,
        )
    )

    request = profile_scheduler._request_queue.get_nowait()
    poll, library_keys, waiters, sources = profile_scheduler._coalesce_requests(request)

    assert poll is False
    assert set(library_keys or []) == {"1", "2", "3"}
    assert len(waiters) == 2
    assert sources == ("api", "webhook")
    first_future.cancel()
    second_future.cancel()


def test_coalesce_requests_drops_keys_when_any_request_targets_full_profile(
    profile_scheduler: ProfileScheduler,
) -> None:
    loop = asyncio.new_event_loop()
    try:
        first = _SyncRequest(
            poll=True,
            library_keys=["1"],
            source="api",
            future=loop.create_future(),
        )
        profile_scheduler._request_queue.put_nowait(
            _SyncRequest(
                poll=True,
                library_keys=None,
                source="manual",
                future=loop.create_future(),
            )
        )

        poll, library_keys, _waiters, sources = profile_scheduler._coalesce_requests(
            first
        )
        assert poll is True
        assert library_keys is None
        assert sources == ("api", "manual")
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_sync_worker_resolves_waiters_and_runtime_metrics(
    profile_scheduler: ProfileScheduler,
) -> None:
    profile_scheduler._running = True
    worker = asyncio.create_task(profile_scheduler._sync_worker())
    future = await profile_scheduler._enqueue_sync(
        poll=False,
        library_keys=["1"],
        source="manual",
    )
    await future
    profile_scheduler.stop_event.set()
    await worker

    metrics = await profile_scheduler.get_runtime_metrics()
    assert metrics["requests_total"] == 1
    assert metrics["last_sync_sources"] == ["manual"]


@pytest.mark.asyncio
async def test_sync_worker_propagates_failures_to_waiters(
    profile_scheduler: ProfileScheduler,
) -> None:
    profile_scheduler._running = True
    _get_bridge(profile_scheduler).behavior = "error"
    worker = asyncio.create_task(profile_scheduler._sync_worker())
    future = await profile_scheduler._enqueue_sync(source="manual")

    with pytest.raises(RuntimeError, match="boom"):
        await future

    profile_scheduler.stop_event.set()
    await worker


@pytest.mark.asyncio
async def test_fail_queued_marks_waiters_with_exception(
    profile_scheduler: ProfileScheduler,
) -> None:
    future = asyncio.get_running_loop().create_future()
    profile_scheduler._request_queue.put_nowait(
        _SyncRequest(
            poll=False,
            library_keys=None,
            source="manual",
            future=future,
        )
    )

    profile_scheduler._fail_queued(RuntimeError("boom"))

    assert future.done() is True
    with pytest.raises(RuntimeError, match="boom"):
        future.result()


@pytest.mark.asyncio
async def test_sync_runs_immediately_without_worker(
    profile_scheduler: ProfileScheduler,
) -> None:
    await profile_scheduler.sync(poll=True, library_keys=["9"], source="manual")

    assert _get_bridge(profile_scheduler).calls == [(True, ["9"])]
    assert profile_scheduler._last_sync_sources == ("manual",)


@pytest.mark.asyncio
@pytest.mark.parametrize("dead_worker", [False, True])
async def test_sync_fallback_serializes_overlapping_requests(
    profile_scheduler: ProfileScheduler,
    monkeypatch: pytest.MonkeyPatch,
    dead_worker: bool,
) -> None:
    if dead_worker:
        profile_scheduler._running = True
        profile_scheduler._worker_task = asyncio.create_task(asyncio.sleep(0))
        await profile_scheduler._worker_task

    active = 0
    max_active = 0
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def sync(*, poll: bool, library_keys: Sequence[str] | None) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            first_started.set()
            await release_first.wait()
        finally:
            active -= 1

    monkeypatch.setattr(profile_scheduler.bridge_client, "sync", sync)
    first = asyncio.create_task(profile_scheduler.sync(source="webhook:scrobble"))
    await first_started.wait()
    second = asyncio.create_task(profile_scheduler.sync(source="webhook:stop"))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        release_first.set()
        await asyncio.gather(first, second)

    assert max_active == 1


@pytest.mark.asyncio
async def test_start_and_stop_manage_worker_and_loops(
    monkeypatch: pytest.MonkeyPatch,
    profile_scheduler_factory,
) -> None:
    scheduler = profile_scheduler_factory(scan_modes=[ScanMode.PERIODIC, ScanMode.POLL])
    spawned: list[tuple[str, int | str, bool]] = []

    def _spawn_loop(*, name: str, interval: int | str, poll: bool) -> None:
        spawned.append((name, interval, poll))

    monkeypatch.setattr(scheduler, "_spawn_loop", _spawn_loop)

    await scheduler.start()
    assert spawned == [("periodic", 120, False), ("poll", 60, True)]

    await scheduler.stop()
    assert scheduler._worker_task is None


@pytest.mark.asyncio
async def test_run_loop_supports_cron_integer_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
    profile_scheduler_factory,
) -> None:
    scheduler = profile_scheduler_factory()
    sync_calls: list[tuple[bool, str]] = []
    waits: list[float] = []

    async def _sync(*, poll: bool, source: str, library_keys=None) -> None:
        sync_calls.append((poll, source))
        scheduler._running = False

    async def _wait_for(awaitable, timeout_duration: float):
        awaitable.close()
        waits.append(timeout_duration)
        raise TimeoutError

    monkeypatch.setattr(scheduler, "sync", _sync)
    monkeypatch.setattr(
        "anibridge.app.core.sched.profile.get_next_interval_seconds",
        lambda interval, now=None: 5,
    )
    monkeypatch.setattr(
        "anibridge.app.core.sched.profile.get_next_run_datetime",
        lambda interval: "later",
    )
    monkeypatch.setattr(
        "anibridge.app.core.sched.profile.human_duration",
        lambda seconds: f"{seconds}s",
    )
    monkeypatch.setattr(asyncio, "wait_for", _wait_for)

    scheduler._running = True
    await scheduler._run_loop(name="poll", interval=60, poll=True)
    assert sync_calls == [(True, "loop:poll")]

    scheduler._running = True

    async def _sync_cron(*, poll: bool, source: str, library_keys=None) -> None:
        sync_calls.append((poll, source))
        scheduler._running = False

    monkeypatch.setattr(scheduler, "sync", _sync_cron)
    await scheduler._run_loop(name="periodic", interval="0 * * * *", poll=False)
    assert sync_calls[-1] == (False, "loop:periodic")

    scheduler._running = True

    async def _boom(*, poll: bool, source: str, library_keys=None) -> None:
        raise RuntimeError("boom")

    async def _sleep(seconds: float) -> None:
        scheduler._running = False

    monkeypatch.setattr(scheduler, "sync", _boom)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    await scheduler._run_loop(name="periodic", interval=60, poll=False)


@pytest.mark.asyncio
async def test_sync_queues_requests_while_worker_is_running(
    profile_scheduler: ProfileScheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_scheduler._running = True
    profile_scheduler._worker_task = asyncio.create_task(asyncio.sleep(3600))

    captured: list[tuple[bool, list[str] | None, str]] = []

    async def _enqueue(
        *,
        poll: bool = False,
        library_keys: Sequence[str] | None = None,
        source: str = "manual",
    ) -> asyncio.Future[None]:
        captured.append(
            (poll, list(library_keys) if library_keys is not None else None, source)
        )
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    monkeypatch.setattr(profile_scheduler, "_enqueue_sync", _enqueue)
    await profile_scheduler.sync(poll=True, library_keys=["7"], source="api")

    assert captured == [(True, ["7"], "api")]
    profile_scheduler._worker_task.cancel()


@pytest.mark.asyncio
async def test_sync_falls_back_when_worker_task_is_dead(
    bridge: _Bridge,
    profile_scheduler: ProfileScheduler,
) -> None:
    """sync() should bypass the dead worker and execute directly."""
    profile_scheduler._running = True
    # Create a worker task that has already completed (simulating a dead worker).
    dead = asyncio.get_running_loop().create_future()
    dead.set_result(None)
    profile_scheduler._worker_task = dead  #  # ty:ignore[invalid-assignment]

    await profile_scheduler.sync(poll=False, library_keys=["k"], source="fallback")

    assert bridge.calls == [(False, ["k"])]


@pytest.mark.asyncio
async def test_on_worker_exit_logs_unexpected_exception(
    profile_scheduler: ProfileScheduler,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Worker exit callback should log errors for unexpected task failures."""
    caplog.set_level(logging.ERROR, logger="anibridge")
    logger = logging.getLogger("anibridge")
    logger.addHandler(caplog.handler)

    try:
        task = asyncio.get_running_loop().create_future()
        task.set_exception(RuntimeError("oops"))

        # Consume the exception to prevent "Future exception was never retrieved".
        with contextlib.suppress(RuntimeError):
            task.result()

        profile_scheduler._on_worker_exit(task)  # ty:ignore[invalid-argument-type]
    finally:
        logger.removeHandler(caplog.handler)

    assert any("Sync worker exited unexpectedly" in r.message for r in caplog.records)
