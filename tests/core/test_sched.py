"""Tests for scheduler components."""

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import anibridge.app.core.sched.client as sched_module
from anibridge.app.config.settings import ScanMode
from anibridge.app.core.sched import ProfileScheduler, SchedulerClient
from anibridge.app.exceptions import ProfileNotFoundError, SchedulerUnavailableError


@dataclass
class FakeProfileConfig:
    """Minimal profile config stub."""

    poll_interval: int = 60
    scan_interval: int = 10
    scan_modes: list[Any] = None  # type: ignore[assignment]
    full_scan: bool = False
    destructive_sync: bool = False

    def __post_init__(self) -> None:
        if self.scan_modes is None:
            self.scan_modes = []


class FakeProvider:
    """Provider stub exposing namespace and optional user."""

    def __init__(self, namespace: str, title: str | None = None) -> None:
        self.NAMESPACE = namespace
        self._user = SimpleNamespace(title=title) if title else None
        self.cleared = False

    def user(self):
        return self._user

    async def clear_cache(self) -> None:
        self.cleared = True


class FakeBridgeClient:
    """Bridge client stub for scheduler tests."""

    def __init__(self, profile_name: str) -> None:
        self.profile_name = profile_name
        self.library_provider = FakeProvider("lib", "LibraryUser")
        self.list_provider = FakeProvider("list", "ListUser")
        self.last_synced: datetime | None = None
        self.current_sync = None
        self.sync_calls: list[tuple[bool, list[str] | None]] = []
        self.closed = False
        self.initialized = False
        self.backed_up = False

    async def initialize(self) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def sync(self, *, poll: bool = False, library_keys=None) -> None:
        self.sync_calls.append((poll, library_keys))

    async def _backup_list(self) -> None:
        self.backed_up = True


class FakeAnimapClient:
    """Animap client stub for scheduler tests."""

    def __init__(self, *_args, **_kwargs) -> None:
        self.initialized = False
        self.closed = False
        self.synced = False

    async def initialize(self) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def sync_db(self) -> None:
        self.synced = True


class FakeConfig:
    """Config stub with profile lookup."""

    def __init__(self, profiles: dict[str, FakeProfileConfig], data_path: Path) -> None:
        self.profiles = profiles
        self.data_path = data_path
        self.mappings_url = None

    def get_profile(self, name: str) -> FakeProfileConfig:
        return self.profiles[name]


def test_profile_scheduler_sync_runs_bridge_sync():
    """ProfileScheduler should call bridge_client.sync."""

    class Bridge:
        def __init__(self) -> None:
            self.calls: list[tuple[bool, list[str] | None]] = []

        async def sync(self, *, poll: bool = False, library_keys=None) -> None:
            self.calls.append((poll, library_keys))

    bridge = Bridge()
    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", bridge),
        scan_interval=1,
        scan_modes=[],
        poll_interval=1,
    )

    asyncio.run(scheduler.sync(poll=True, library_keys=["a"]))

    assert bridge.calls == [(True, ["a"])]


@pytest.mark.asyncio
async def test_profile_scheduler_start_spawns_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start should spawn periodic and poll loops when enabled."""
    calls: list[tuple[str, int, bool]] = []

    class Bridge:
        async def sync(self, *, poll: bool = False, library_keys=None) -> None:
            return None

    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", Bridge()),
        scan_interval=5,
        scan_modes=[ScanMode.PERIODIC, ScanMode.POLL],
        poll_interval=2,
    )

    def _spawn_loop(*, name: str, interval: int, poll: bool) -> None:
        calls.append((name, interval, poll))

    monkeypatch.setattr(scheduler, "_spawn_loop", _spawn_loop)

    await scheduler.start()

    assert calls == [("periodic", 5, False), ("poll", 2, True)]


@pytest.mark.asyncio
async def test_profile_scheduler_stop_cancels_tasks() -> None:
    """Stop should cancel background tasks."""
    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", SimpleNamespace()),
        scan_interval=1,
        scan_modes=[],
        poll_interval=1,
    )

    start_event = asyncio.Event()

    async def _waiter():
        start_event.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_waiter())
    await start_event.wait()
    scheduler._tasks.add(task)

    await scheduler.stop()

    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_profile_scheduler_sync_cancellation() -> None:
    """Cancellation should propagate and clear current task."""
    start_event = asyncio.Event()

    class Bridge:
        async def sync(self, *, poll: bool = False, library_keys=None) -> None:
            start_event.set()
            await asyncio.Event().wait()

    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", Bridge()),
        scan_interval=1,
        scan_modes=[],
        poll_interval=1,
    )

    task = asyncio.create_task(scheduler.sync())
    await start_event.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert scheduler._current_task is None


@pytest.mark.asyncio
async def test_profile_scheduler_run_loop_stops_on_event(
    monkeypatch: pytest.MonkeyPatch,
):
    """Loop should stop once stop_event is set."""
    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", SimpleNamespace()),
        scan_interval=1,
        scan_modes=[],
        poll_interval=1,
    )

    async def _sync(*_args, **_kwargs) -> None:
        scheduler.stop_event.set()

    monkeypatch.setattr(scheduler, "sync", _sync)

    scheduler._running = True
    await scheduler._run_loop(name="periodic", interval=1, poll=False)

    assert scheduler.stop_event.is_set()


@pytest.mark.asyncio
async def test_profile_scheduler_run_loop_handles_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors inside the loop should be caught and retried."""
    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", SimpleNamespace()),
        scan_interval=1,
        scan_modes=[],
        poll_interval=1,
    )

    async def _boom(*_args, **_kwargs) -> None:
        scheduler.stop_event.set()
        raise RuntimeError("boom")

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(scheduler, "sync", _boom)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    scheduler._running = True
    await scheduler._run_loop(name="poll", interval=1, poll=True)

    assert scheduler.stop_event.is_set()


@pytest.mark.asyncio
async def test_scheduler_initialize_and_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scheduler should initialize animap and bridge clients."""
    profiles = {
        "good": FakeProfileConfig(scan_modes=[]),
        "bad": FakeProfileConfig(scan_modes=[]),
    }
    config = FakeConfig(profiles=profiles, data_path=tmp_path)

    created: list[FakeBridgeClient] = []

    def fake_bridge_client(profile_name: str, *_args, **_kwargs):
        client = FakeBridgeClient(profile_name)
        if profile_name == "bad":
            raise RuntimeError("boom")
        created.append(client)
        return client

    monkeypatch.setattr(sched_module, "AnimapClient", FakeAnimapClient)
    monkeypatch.setattr(sched_module, "BridgeClient", fake_bridge_client)

    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    await scheduler.initialize()

    assert cast(FakeAnimapClient, scheduler.shared_animap_client).initialized is True
    assert "good" in scheduler.bridge_clients
    assert "bad" not in scheduler.bridge_clients


@pytest.mark.asyncio
async def test_scheduler_start_and_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Start should create profile schedulers and stop should close resources."""
    profiles = {"good": FakeProfileConfig(scan_modes=[])}
    config = FakeConfig(profiles=profiles, data_path=tmp_path)

    monkeypatch.setattr(sched_module, "AnimapClient", FakeAnimapClient)

    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    cast(dict[str, Any], scheduler.bridge_clients)["good"] = FakeBridgeClient("good")

    class StubScheduler:
        def __init__(self, *_, **__):
            self._running = False

        async def start(self) -> None:
            self._running = True

        async def stop(self) -> None:
            self._running = False

    monkeypatch.setattr(sched_module, "ProfileScheduler", StubScheduler)

    await scheduler.start()
    assert scheduler.is_running is True
    assert scheduler.profile_schedulers

    await scheduler.stop()
    assert scheduler.is_running is False
    assert cast(FakeAnimapClient, scheduler.shared_animap_client).closed is True
    assert not scheduler.bridge_clients


@pytest.mark.asyncio
async def test_scheduler_trigger_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Manual sync should target requested profiles or all."""
    profiles = {
        "one": FakeProfileConfig(scan_modes=[]),
        "two": FakeProfileConfig(scan_modes=[]),
    }
    config = FakeConfig(profiles=profiles, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))

    cast(dict[str, Any], scheduler.bridge_clients)["one"] = FakeBridgeClient("one")
    cast(dict[str, Any], scheduler.bridge_clients)["two"] = FakeBridgeClient("two")

    class StubScheduler:
        def __init__(self) -> None:
            self.calls: list[tuple[bool, list[str] | None, str]] = []

        async def sync(
            self,
            *,
            poll: bool = False,
            library_keys=None,
            source: str = "manual",
        ) -> None:
            self.calls.append((poll, library_keys, source))

    cast(dict[str, Any], scheduler.profile_schedulers)["one"] = StubScheduler()
    cast(dict[str, Any], scheduler.profile_schedulers)["two"] = StubScheduler()

    await scheduler.trigger_profile_sync("one", poll=True, library_keys=["x"])

    assert cast(StubScheduler, scheduler.profile_schedulers["one"]).calls == [
        (True, ["x"], "manual")
    ]

    await scheduler.trigger_all_profiles_sync(poll=False, library_keys=None)

    assert cast(StubScheduler, scheduler.profile_schedulers["two"]).calls == [
        (False, None, "manual")
    ]

    with pytest.raises(ProfileNotFoundError):
        await scheduler.trigger_profile_sync("missing")


@pytest.mark.asyncio
async def test_scheduler_trigger_profile_sync_without_running_scheduler(
    tmp_path: Path,
) -> None:
    """Manual trigger should fall back to a one-off bridge sync when not started."""
    profiles = {"one": FakeProfileConfig(scan_modes=[])}
    config = FakeConfig(profiles=profiles, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))

    bridge = FakeBridgeClient("one")
    cast(dict[str, Any], scheduler.bridge_clients)["one"] = bridge

    await scheduler.trigger_profile_sync("one", poll=True, library_keys=["k1"])

    assert bridge.sync_calls == [(True, ["k1"])]


@pytest.mark.asyncio
async def test_scheduler_trigger_all_profiles_sync_raises_on_failures(
    tmp_path: Path,
) -> None:
    """Aggregated trigger should raise if any profile sync fails."""
    profiles = {
        "good": FakeProfileConfig(scan_modes=[]),
        "bad": FakeProfileConfig(scan_modes=[]),
    }
    config = FakeConfig(profiles=profiles, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))

    class GoodBridge(FakeBridgeClient):
        async def sync(self, *, poll: bool = False, library_keys=None) -> None:
            self.sync_calls.append((poll, library_keys))

    class BadBridge(FakeBridgeClient):
        async def sync(self, *, poll: bool = False, library_keys=None) -> None:
            raise RuntimeError("boom")

    cast(dict[str, Any], scheduler.bridge_clients)["good"] = GoodBridge("good")
    cast(dict[str, Any], scheduler.bridge_clients)["bad"] = BadBridge("bad")

    with pytest.raises(ExceptionGroup):
        await scheduler.trigger_all_profiles_sync(source="test:all")

    assert cast(GoodBridge, scheduler.bridge_clients["good"]).sync_calls == [
        (False, None)
    ]


@pytest.mark.asyncio
async def test_profile_scheduler_metrics_include_pending_and_last_sources() -> None:
    """Runtime metrics should expose mailbox state and source attribution."""

    class Bridge:
        async def sync(self, *, poll: bool = False, library_keys=None) -> None:
            return None

    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", Bridge()),
        scan_interval=1,
        scan_modes=[],
        poll_interval=1,
    )

    await scheduler.sync(source="test:manual")
    metrics = await scheduler.get_runtime_metrics()

    assert metrics["last_sync_sources"] == ["test:manual"]
    assert metrics["requests_total"] == 0
    assert metrics["requests_rejected"] == 0


@pytest.mark.asyncio
async def test_profile_scheduler_rejects_when_pending_waiters_full() -> None:
    """Enqueue should reject when pending waiters exceed configured limit."""

    class Bridge:
        async def sync(self, *, poll: bool = False, library_keys=None) -> None:
            return None

    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", Bridge()),
        scan_interval=1,
        scan_modes=[],
        poll_interval=1,
        max_pending_waiters=1,
    )
    scheduler._running = True
    scheduler._worker_task = asyncio.create_task(asyncio.sleep(10))

    first = await scheduler._enqueue_sync(source="test:first")
    assert not first.done()

    with pytest.raises(SchedulerUnavailableError):
        await scheduler._enqueue_sync(source="test:second")

    metrics = await scheduler.get_runtime_metrics()
    assert metrics["requests_total"] == 1
    assert metrics["requests_rejected"] == 1

    scheduler._worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler._worker_task


def test_scheduler_get_next_database_sync_at(tmp_path: Path) -> None:
    """Next sync time should be None when not running."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))

    assert scheduler.get_next_database_sync_at() is None

    scheduler._running = True
    next_time = scheduler.get_next_database_sync_at()
    assert next_time is not None


@pytest.mark.asyncio
async def test_scheduler_get_status(tmp_path: Path) -> None:
    """Status should include profile and runtime data."""
    profiles = {"one": FakeProfileConfig(scan_modes=[])}
    config = FakeConfig(profiles=profiles, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))

    bridge = FakeBridgeClient("one")
    bridge.last_synced = datetime(2025, 1, 1, tzinfo=UTC)
    cast(dict[str, Any], scheduler.bridge_clients)["one"] = bridge

    class SchedulerStub:
        _running = True

        async def get_runtime_metrics(self) -> dict[str, Any]:
            return {"requests_total": 0}

    scheduler.profile_schedulers["one"] = cast(
        "sched_module.ProfileScheduler", SchedulerStub()
    )

    status = await scheduler.get_status()

    assert status["one"]["config"]["library_namespace"] == "Lib"
    assert status["one"]["status"]["last_synced"] == "2025-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_daily_db_sync_loop_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Daily loop should invoke sync and backups."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    scheduler.shared_animap_client = FakeAnimapClient()
    cast(dict[str, Any], scheduler.bridge_clients)["one"] = FakeBridgeClient("one")

    scheduler._running = True

    def _next_sync(_now: datetime) -> datetime:
        return datetime.now(UTC)

    async def _fast_wait(coro, *_args, **_kwargs):
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(scheduler, "_get_next_1am_utc", _next_sync)
    monkeypatch.setattr(asyncio, "wait_for", _fast_wait)

    async def _sync_db() -> None:
        cast(FakeAnimapClient, scheduler.shared_animap_client).synced = True
        scheduler.stop_event.set()

    monkeypatch.setattr(scheduler.shared_animap_client, "sync_db", _sync_db)

    await scheduler._daily_db_sync_loop()

    assert scheduler.shared_animap_client.synced is True
    assert cast(FakeBridgeClient, scheduler.bridge_clients["one"]).backed_up is True


@pytest.mark.asyncio
async def test_trigger_database_sync_runs_refresh(tmp_path: Path) -> None:
    """Database sync entrypoint should sync mappings and refresh profile providers."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    scheduler.shared_animap_client = FakeAnimapClient()

    bridge = FakeBridgeClient("one")
    cast(dict[str, Any], scheduler.bridge_clients)["one"] = bridge

    await scheduler.trigger_database_sync(source="test:database")

    assert scheduler.shared_animap_client.synced is True
    assert bridge.list_provider.cleared is True
    assert bridge.backed_up is True


@pytest.mark.asyncio
async def test_scheduler_runtime_metrics_include_coordinator(tmp_path: Path) -> None:
    """Scheduler runtime metrics should include global coordinator counters."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))

    metrics = await scheduler.get_runtime_metrics()

    assert metrics["running"] is False
    assert metrics["profile_count"] == 0
    assert metrics["bridge_count"] == 0
    assert metrics["daily_sync_active"] is False

    coordinator = metrics["coordinator"]
    assert coordinator["active_profile_syncs"] == 0
    assert coordinator["maintenance_active"] is False
    assert coordinator["maintenance_waiting"] == 0


def test_get_profiles_for_library_provider(tmp_path: Path) -> None:
    """Profiles should be grouped by library provider namespace."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    cast(dict[str, Any], scheduler.bridge_clients)["one"] = FakeBridgeClient("one")

    scheduler.get_profiles_for_library_provider.cache_clear()

    profiles = scheduler.get_profiles_for_library_provider("lib")
    assert profiles == ["one"]

    scheduler.get_profiles_for_library_provider.cache_clear()

    with pytest.raises(ProfileNotFoundError):
        scheduler.get_profiles_for_library_provider("missing")


def test_request_shutdown_sets_event(tmp_path: Path) -> None:
    """Request shutdown should set the stop event."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))

    scheduler.request_shutdown()

    assert scheduler.stop_event.is_set()


def test_get_next_1am_utc_rolls_over(tmp_path: Path) -> None:
    """Next 1AM UTC should roll over after 1AM."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))

    now = datetime(2025, 1, 1, 2, 0, tzinfo=UTC)
    next_time = scheduler._get_next_1am_utc(now)

    assert next_time.day == 2
    assert next_time.hour == 1


@pytest.mark.asyncio
async def test_wait_for_completion_returns_when_stopped(tmp_path: Path) -> None:
    """wait_for_completion should return when stop_event is set."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    scheduler._running = True

    scheduler.stop_event.set()
    await scheduler.wait_for_completion()


@pytest.mark.asyncio
async def test_profile_scheduler_sync_cancels_inner_task() -> None:
    """Cancelling sync should cancel the in-flight bridge task."""
    started = asyncio.Event()

    class Bridge:
        def __init__(self) -> None:
            self.cancelled = False

        async def sync(self, *, poll: bool = False, library_keys=None) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    bridge = Bridge()
    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", bridge),
        scan_interval=1,
        scan_modes=[],
        poll_interval=1,
    )

    task = asyncio.create_task(scheduler.sync())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert bridge.cancelled is True
    assert scheduler._current_task is None


@pytest.mark.asyncio
async def test_profile_scheduler_start_returns_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling start while running should be a no-op."""
    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", SimpleNamespace()),
        scan_interval=1,
        scan_modes=[ScanMode.PERIODIC],
        poll_interval=1,
    )
    scheduler._running = True

    def _spawn_loop(*_args, **_kwargs):
        raise AssertionError("spawn loop should not be called")

    monkeypatch.setattr(scheduler, "_spawn_loop", _spawn_loop)

    await scheduler.start()


@pytest.mark.asyncio
async def test_profile_scheduler_stop_cancels_current_task() -> None:
    """Stopping should cancel a running current task."""
    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", SimpleNamespace()),
        scan_interval=1,
        scan_modes=[],
        poll_interval=1,
    )

    async def _waiter():
        await asyncio.Event().wait()

    scheduler._current_task = asyncio.create_task(_waiter())
    await scheduler.stop()

    assert scheduler._current_task.cancelled() or scheduler._current_task.done()


@pytest.mark.asyncio
async def test_profile_scheduler_run_loop_cancellation() -> None:
    """Cancelling a loop task should hit cancellation handling."""
    scheduler = ProfileScheduler(
        profile_name="default",
        bridge_client=cast("sched_module.BridgeClient", SimpleNamespace()),
        scan_interval=1,
        scan_modes=[],
        poll_interval=1,
    )

    async def _sync(*_args, **_kwargs) -> None:
        await asyncio.Event().wait()

    scheduler._running = True
    scheduler.sync = _sync  # type: ignore[method-assign]

    task = asyncio.create_task(
        scheduler._run_loop(name="periodic", interval=1, poll=False)
    )
    await asyncio.sleep(0)
    task.cancel()
    await task


@pytest.mark.asyncio
async def test_scheduler_start_with_scan_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Start should compute next sync time when scan modes are enabled."""
    profiles = {"good": FakeProfileConfig(scan_modes=[ScanMode.PERIODIC])}
    config = FakeConfig(profiles=profiles, data_path=tmp_path)

    class StubScheduler:
        def __init__(self, *_, **__):
            self._running = False

        async def start(self) -> None:
            self._running = True

        async def stop(self) -> None:
            self._running = False

    monkeypatch.setattr(sched_module, "AnimapClient", FakeAnimapClient)
    monkeypatch.setattr(sched_module, "ProfileScheduler", StubScheduler)

    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    cast(dict[str, Any], scheduler.bridge_clients)["good"] = FakeBridgeClient("good")

    await scheduler.start()

    assert scheduler.profile_schedulers

    await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_start_without_profiles(tmp_path: Path) -> None:
    """Starting without profiles should leave schedulers empty."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))

    await scheduler.start()

    assert scheduler.profile_schedulers == {}

    await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_stop_returns_when_not_running(tmp_path: Path) -> None:
    """Stop should no-op when scheduler is not running."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))

    await scheduler.stop()

    assert scheduler.is_running is False


@pytest.mark.asyncio
async def test_wait_for_completion_cancelled(tmp_path: Path) -> None:
    """Cancelling wait_for_completion should propagate the cancellation."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    scheduler._running = True

    task = asyncio.create_task(scheduler.wait_for_completion())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_daily_db_sync_loop_breaks_on_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Daily loop should break when stop event is signaled during wait."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    scheduler._running = True

    async def _wait_for(coro, *_args, **_kwargs):
        scheduler.stop_event.set()
        return await coro

    monkeypatch.setattr(asyncio, "wait_for", _wait_for)

    await scheduler._daily_db_sync_loop()


@pytest.mark.asyncio
async def test_daily_db_sync_loop_handles_sync_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Errors during sync_db should be handled and logged."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    scheduler.shared_animap_client = FakeAnimapClient()
    scheduler._running = True

    def _next_sync(_now: datetime) -> datetime:
        return datetime.now(UTC)

    async def _fast_wait(coro, *_args, **_kwargs):
        coro.close()
        raise TimeoutError

    async def _sync_db() -> None:
        scheduler.stop_event.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler, "_get_next_1am_utc", _next_sync)
    monkeypatch.setattr(asyncio, "wait_for", _fast_wait)
    monkeypatch.setattr(scheduler.shared_animap_client, "sync_db", _sync_db)

    await scheduler._daily_db_sync_loop()


@pytest.mark.asyncio
async def test_daily_db_sync_loop_handles_loop_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unexpected errors in the loop should trigger retry sleep."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    scheduler._running = True

    def _boom(_now: datetime) -> datetime:
        scheduler._running = False
        raise RuntimeError("boom")

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(scheduler, "_get_next_1am_utc", _boom)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    await scheduler._daily_db_sync_loop()


def test_get_profiles_for_library_provider_skips_none(tmp_path: Path) -> None:
    """None bridge clients should be ignored when grouping profiles."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    cast(dict[str, Any], scheduler.bridge_clients)["none"] = None
    cast(dict[str, Any], scheduler.bridge_clients)["good"] = FakeBridgeClient("good")

    scheduler.get_profiles_for_library_provider.cache_clear()

    profiles = scheduler.get_profiles_for_library_provider("lib")
    assert profiles == ["good"]


@pytest.mark.asyncio
async def test_scheduler_context_manager_calls_stop(tmp_path: Path) -> None:
    """Async context manager should call stop on exit."""
    config = FakeConfig(profiles={}, data_path=tmp_path)
    scheduler = SchedulerClient(cast("sched_module.AnibridgeConfig", config))
    called = {"stop": False}

    async def _stop() -> None:
        called["stop"] = True

    scheduler.stop = _stop  # type: ignore[method-assign]

    async with scheduler:
        pass

    assert called["stop"] is True
