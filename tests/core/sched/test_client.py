"""Tests for the single-queue scheduler client."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from anibridge.provider.base import Ref

import anibridge.app.core.sched.client as sched_module
from anibridge.app.config.settings import AnibridgeConfig
from anibridge.app.core.bridge import BridgeClient
from anibridge.app.core.sched.client import SchedulerClient
from anibridge.app.core.sync import RecordUndoRequest, SyncRequest, SyncTrigger
from anibridge.app.exceptions import ProfileNotFoundError


class FakeAnimapClient:
    """Shared AniMap client stub."""

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


class FakeProvider:
    """Provider stub with namespace and account metadata."""

    def __init__(self, namespace: str, title: str) -> None:
        self.NAMESPACE = namespace
        self._account = SimpleNamespace(title=title)

    def account(self):
        return self._account


class FakeBridge:
    """Bridge client stub used by the scheduler."""

    def __init__(self) -> None:
        self.source_provider = FakeProvider("source", "Source")
        self.target_provider = FakeProvider("target", "Target")
        self.last_synced = None
        self.current_sync = None
        self.sync_calls: list[SyncRequest] = []
        self.closed = False
        self.backed_up = False
        self.sync_error: Exception | None = None
        self.backup_error: Exception | None = None

    async def sync(self, *, request: SyncRequest) -> None:
        if self.sync_error is not None:
            raise self.sync_error
        self.sync_calls.append(request)

    async def close(self) -> None:
        self.closed = True

    async def _backup_target(self) -> None:
        if self.backup_error is not None:
            raise self.backup_error
        self.backed_up = True


class FakeConfig:
    """Minimal global config for SchedulerClient tests."""

    def __init__(self, tmp_path: Path) -> None:
        self.data_path = tmp_path
        self.mappings_url = None
        self.profiles = {
            "default": SimpleNamespace(
                scan_modes=[],
                scan_interval=60,
                poll_interval=30,
                full_scan=False,
                destructive_sync=False,
            )
        }

    def get_profile(self, name: str):
        return self.profiles[name]


@pytest.fixture()
def scheduler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SchedulerClient:
    """Build a scheduler with stubbed external clients."""
    monkeypatch.setattr(sched_module, "AnimapClient", FakeAnimapClient)
    client = SchedulerClient(cast(AnibridgeConfig, FakeConfig(tmp_path)))
    client.bridge_clients["default"] = cast(BridgeClient, FakeBridge())
    return client


def test_coalesced_request_preserves_full_manual_scan() -> None:
    """Manual full scans should dominate queued targeted refs."""
    request = SchedulerClient._coalesced_request(
        [
            SyncRequest(trigger=SyncTrigger.WEBHOOK, source_refs=(Ref.anchor("a"),)),
            SyncRequest(trigger=SyncTrigger.MANUAL),
            SyncRequest(trigger=SyncTrigger.WEBHOOK, source_refs=(Ref.anchor("b"),)),
        ]
    )

    assert request.trigger == SyncTrigger.MANUAL
    assert request.source_refs is None


def test_coalesced_request_deduplicates_targeted_refs() -> None:
    """Targeted queue coalescing should preserve first-seen ref order."""
    request = SchedulerClient._coalesced_request(
        [
            SyncRequest(
                trigger=SyncTrigger.WEBHOOK,
                source_refs=(Ref.anchor("a"), Ref.anchor("b")),
            ),
            SyncRequest(
                trigger=SyncTrigger.WEBHOOK,
                source_refs=(Ref.anchor("a"), Ref.anchor("c")),
            ),
        ]
    )

    assert request.trigger == SyncTrigger.WEBHOOK
    assert request.source_refs == (Ref.anchor("a"), Ref.anchor("b"), Ref.anchor("c"))


def test_coalesced_request_preserves_record_undos() -> None:
    """Queued undo requests should survive request coalescing."""
    undo = RecordUndoRequest(
        source_ref=Ref.anchor("source"),
        target_ref=Ref.anchor("target"),
        before=None,
        after=None,
    )

    request = SchedulerClient._coalesced_request(
        [
            SyncRequest(trigger=SyncTrigger.MANUAL),
            SyncRequest(
                trigger=SyncTrigger.MANUAL,
                record_undos=(undo,),
                source_refs=(),
            ),
        ]
    )

    assert request.source_refs is None
    assert request.record_undos == (undo,)


def test_coalesced_request_preserves_poll_fallback_coverage() -> None:
    """Broad polls should retain fallback coverage when targeted refs merge in."""
    poll_with_target = SchedulerClient._coalesced_request(
        [
            SyncRequest(trigger=SyncTrigger.WEBHOOK, source_refs=(Ref.anchor("a"),)),
            SyncRequest(trigger=SyncTrigger.POLL),
        ]
    )

    assert poll_with_target.trigger == SyncTrigger.POLL
    assert poll_with_target.source_refs == (Ref.anchor("a"),)
    assert poll_with_target.full_scan_on_poll_fallback is True

    manual_with_poll = SchedulerClient._coalesced_request(
        [
            SyncRequest(trigger=SyncTrigger.MANUAL, source_refs=(Ref.anchor("a"),)),
            SyncRequest(trigger=SyncTrigger.POLL),
        ]
    )

    assert manual_with_poll.trigger == SyncTrigger.MANUAL
    assert manual_with_poll.source_refs is None
    assert manual_with_poll.full_scan_on_poll_fallback is False


@pytest.mark.asyncio
async def test_trigger_profile_sync_runs_immediately_when_stopped(
    scheduler: SchedulerClient,
) -> None:
    """A stopped scheduler should execute manual syncs directly."""
    request = SyncRequest(trigger=SyncTrigger.WEBHOOK, source_refs=(Ref.anchor("a"),))

    await scheduler.trigger_profile_sync("default", request=request, source="test")

    bridge = scheduler.bridge_clients["default"]
    assert isinstance(bridge, FakeBridge)
    assert bridge.sync_calls == [request]


@pytest.mark.asyncio
async def test_get_status_reports_single_queue_scheduler(
    scheduler: SchedulerClient,
) -> None:
    """Status should expose provider metadata and queue metrics."""
    status = await scheduler.get_status()

    profile = status["default"]
    assert profile["config"]["source_namespace"] == "source"
    assert profile["config"]["target_namespace"] == "target"
    assert profile["status"]["scheduler"]["pending_waiters"] == 0
    assert profile["status"]["scheduler"]["sync_active"] is False


def test_get_profiles_for_source_provider_uses_bridge_sources(
    scheduler: SchedulerClient,
) -> None:
    """Webhook routing should find profiles by source provider namespace."""
    assert scheduler.get_profiles_for_source_provider("source") == ["default"]


@pytest.mark.asyncio
async def test_trigger_database_sync_runs_database_and_backups(
    scheduler: SchedulerClient,
) -> None:
    """Maintenance sync should run AniMap sync and target backups under one lock."""
    await scheduler.trigger_database_sync(source="test")

    assert isinstance(scheduler.shared_animap_client, FakeAnimapClient)
    assert scheduler.shared_animap_client.synced is True
    bridge = scheduler.bridge_clients["default"]
    assert isinstance(bridge, FakeBridge)
    assert bridge.backed_up is True


@pytest.mark.asyncio
async def test_start_without_scan_modes_starts_only_global_tasks(
    scheduler: SchedulerClient,
) -> None:
    """Profiles without scan modes should not create producer tasks."""
    await scheduler.start()
    try:
        assert scheduler.is_running is True
        assert len(scheduler._producer_tasks) == 0
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_initialize_tracks_success_and_failed_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Initialization should keep usable profiles and record failed profiles."""
    monkeypatch.setattr(sched_module, "AnimapClient", FakeAnimapClient)
    monkeypatch.setattr(sched_module, "release_memory", lambda: None)

    class InitBridge(FakeBridge):
        def __init__(self, *, profile_name: str, **_kwargs) -> None:
            super().__init__()
            self.profile_name = profile_name

        async def initialize(self) -> None:
            if self.profile_name == "broken":
                raise RuntimeError("bad profile")

    config = FakeConfig(tmp_path)
    config.profiles["broken"] = SimpleNamespace(
        scan_modes=[],
        scan_interval=60,
        poll_interval=30,
        full_scan=False,
        destructive_sync=False,
    )
    monkeypatch.setattr(sched_module, "BridgeClient", InitBridge)
    scheduler = SchedulerClient(cast(AnibridgeConfig, config))

    await scheduler.initialize()

    assert isinstance(scheduler.shared_animap_client, FakeAnimapClient)
    assert scheduler.shared_animap_client.initialized is True
    assert set(scheduler.bridge_clients) == {"default"}
    assert scheduler.failed_profile_errors == {"broken": "bad profile"}


@pytest.mark.asyncio
async def test_initialize_bridge_client_closes_partial_client_on_failure(
    scheduler: SchedulerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[FakeBridge] = []

    class BrokenBridge(FakeBridge):
        def __init__(self, **_kwargs) -> None:
            super().__init__()

        async def initialize(self) -> None:
            raise RuntimeError("init failed")

        async def close(self) -> None:
            closed.append(self)
            await super().close()

    monkeypatch.setattr(sched_module, "BridgeClient", BrokenBridge)
    with pytest.raises(RuntimeError, match="init failed"):
        await scheduler._initialize_bridge_client("default", object())
    assert len(closed) == 1
    assert closed[0].closed is True


def test_start_profile_producers_uses_enabled_scan_modes(
    scheduler: SchedulerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, SyncTrigger]] = []
    profile = scheduler.global_config.get_profile("default")
    profile.scan_modes = [sched_module.ScanMode.PERIODIC, sched_module.ScanMode.POLL]

    def spawn(**kwargs) -> None:
        calls.append((kwargs["name"], kwargs["request"].trigger))

    monkeypatch.setattr(scheduler, "_spawn_producer", spawn)

    scheduler._start_profile_producers("default")

    assert calls == [
        ("periodic", SyncTrigger.PERIODIC),
        ("poll", SyncTrigger.POLL),
    ]


def test_lifecycle_helpers_report_shutdown_and_next_database_sync(
    scheduler: SchedulerClient,
) -> None:
    assert scheduler.get_next_database_sync_at() is None
    scheduler._running = True
    assert scheduler.get_next_database_sync_at() is not None
    assert scheduler._get_next_1am_utc(
        datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    ) == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert scheduler._get_next_1am_utc(
        datetime(2026, 1, 1, 1, 30, tzinfo=UTC)
    ) == datetime(2026, 1, 2, 1, tzinfo=UTC)

    scheduler.request_shutdown()
    scheduler.request_shutdown()
    assert scheduler.stop_event.is_set() is True


@pytest.mark.asyncio
async def test_spawn_and_cancel_profile_producer(
    scheduler: SchedulerClient,
) -> None:
    scheduler._running = True
    scheduler._spawn_producer(
        profile_name="default",
        name="poll",
        interval=3600,
        request=SyncRequest(trigger=SyncTrigger.POLL),
    )
    task = next(iter(scheduler._producer_tasks))
    assert task.get_name() == "profile:default:poll"

    scheduler._cancel_profile_producers("default")
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() is True


@pytest.mark.asyncio
async def test_producer_loop_triggers_once_and_respects_shutdown(
    scheduler: SchedulerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, SyncTrigger, str]] = []
    scheduler._running = True
    monkeypatch.setattr(sched_module, "get_next_interval_seconds", lambda *_args: 0)

    async def trigger(profile_name: str, *, request: SyncRequest, source: str) -> None:
        calls.append((profile_name, request.trigger, source))
        scheduler.request_shutdown()

    monkeypatch.setattr(scheduler, "trigger_profile_sync", trigger)

    await scheduler._producer_loop(
        profile_name="default",
        name="poll",
        interval=30,
        request=SyncRequest(trigger=SyncTrigger.POLL),
    )

    assert calls == [("default", SyncTrigger.POLL, "loop:poll")]


@pytest.mark.asyncio
async def test_producer_loop_logs_errors_and_cancelled_runs(
    scheduler: SchedulerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler._running = True
    monkeypatch.setattr(sched_module, "get_next_interval_seconds", lambda *_args: 0)
    monkeypatch.setattr(sched_module, "get_next_run_datetime", lambda *_args: "now")

    async def trigger_error(*_args, **_kwargs) -> None:
        scheduler.request_shutdown()
        raise RuntimeError("producer boom")

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(scheduler, "trigger_profile_sync", trigger_error)
    monkeypatch.setattr(sched_module.asyncio, "sleep", fake_sleep)

    await scheduler._producer_loop(
        profile_name="default",
        name="poll",
        interval="* * * * *",
        request=SyncRequest(trigger=SyncTrigger.POLL),
    )

    scheduler.stop_event = asyncio.Event()
    scheduler._running = True

    async def trigger_cancel(*_args, **_kwargs) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "trigger_profile_sync", trigger_cancel)
    await scheduler._producer_loop(
        profile_name="default",
        name="poll",
        interval=30,
        request=SyncRequest(trigger=SyncTrigger.POLL),
    )


@pytest.mark.asyncio
async def test_stop_cancels_background_tasks_and_pending_queue(
    scheduler: SchedulerClient,
) -> None:
    scheduler._running = True
    scheduler._producer_tasks.add(asyncio.create_task(asyncio.sleep(60)))
    scheduler._daily_sync_task = asyncio.create_task(asyncio.sleep(60))
    scheduler._sync_worker_task = asyncio.create_task(asyncio.sleep(60))
    pending = scheduler._enqueue_sync(
        profile_name="default",
        request=SyncRequest(),
        source="api",
    )

    await scheduler.stop()

    bridge = scheduler.bridge_clients.get("default")
    assert bridge is None
    assert isinstance(scheduler.shared_animap_client, FakeAnimapClient)
    assert scheduler.shared_animap_client.closed is True
    with pytest.raises(asyncio.CancelledError):
        await pending


@pytest.mark.asyncio
async def test_sync_worker_completes_queued_requests(
    scheduler: SchedulerClient,
) -> None:
    request = SyncRequest(trigger=SyncTrigger.WEBHOOK, source_refs=(Ref.anchor("a"),))
    scheduler._running = True
    worker = asyncio.create_task(scheduler._sync_worker())
    scheduler._sync_worker_task = worker
    try:
        await scheduler.trigger_profile_sync("default", request=request, source="api")
        bridge = scheduler.bridge_clients["default"]
        assert isinstance(bridge, FakeBridge)
        assert bridge.sync_calls == [request]
        assert scheduler._last_sync_sources["default"] == ("api",)
        assert scheduler._pending_sync_counts == {}
    finally:
        scheduler.request_shutdown()
        await worker


@pytest.mark.asyncio
async def test_sync_worker_propagates_profile_errors(
    scheduler: SchedulerClient,
) -> None:
    bridge = scheduler.bridge_clients["default"]
    assert isinstance(bridge, FakeBridge)
    bridge.sync_error = RuntimeError("sync boom")
    scheduler._running = True
    worker = asyncio.create_task(scheduler._sync_worker())
    scheduler._sync_worker_task = worker
    try:
        with pytest.raises(RuntimeError, match="sync boom"):
            await scheduler.trigger_profile_sync("default", source="api")
    finally:
        scheduler.request_shutdown()
        await worker


@pytest.mark.asyncio
async def test_sync_worker_cancels_waiter_when_stopped_with_ready_queue(
    scheduler: SchedulerClient,
) -> None:
    scheduler._running = True
    future = scheduler._enqueue_sync(
        profile_name="default",
        request=SyncRequest(),
        source="api",
    )
    scheduler.request_shutdown()

    await scheduler._sync_worker()

    with pytest.raises(asyncio.CancelledError):
        await future


@pytest.mark.asyncio
async def test_queue_helpers_reject_coalesce_and_fail_pending(
    scheduler: SchedulerClient,
) -> None:
    scheduler._sync_queue = asyncio.Queue(maxsize=1)
    future = scheduler._enqueue_sync(
        profile_name="default",
        request=SyncRequest(
            trigger=SyncTrigger.WEBHOOK,
            source_refs=(Ref.anchor("a"),),
        ),
        source="one",
    )
    with pytest.raises(sched_module.SchedulerUnavailableError):
        scheduler._enqueue_sync(
            profile_name="default",
            request=SyncRequest(),
            source="two",
        )
    assert scheduler._sync_requests_rejected == 1

    scheduler._fail_queued(RuntimeError("shutdown"))
    with pytest.raises(RuntimeError, match="shutdown"):
        await future
    assert scheduler._pending_sync_counts == {}

    scheduler._sync_queue = asyncio.Queue()
    first = scheduler._enqueue_sync(
        profile_name="default",
        request=SyncRequest(
            trigger=SyncTrigger.WEBHOOK,
            source_refs=(Ref.anchor("a"),),
        ),
        source="api",
    )
    scheduler._enqueue_sync(
        profile_name="default",
        request=SyncRequest(trigger=SyncTrigger.POLL),
        source="poll",
    )
    scheduler.bridge_clients["other"] = cast(BridgeClient, FakeBridge())
    scheduler._enqueue_sync(
        profile_name="other",
        request=SyncRequest(trigger=SyncTrigger.MANUAL),
        source="manual",
    )
    first_item = scheduler._sync_queue.get_nowait()
    request, waiters, sources = scheduler._coalesce_profile_requests(first_item)

    assert request.trigger == SyncTrigger.POLL
    assert request.source_refs == (Ref.anchor("a"),)
    assert request.full_scan_on_poll_fallback is True
    assert waiters[0] is first
    assert sources == ("api", "poll")
    assert scheduler._pending_sync_counts == {"other": 1}
    assert scheduler._sync_queue.qsize() == 1


@pytest.mark.asyncio
async def test_trigger_all_profiles_sync_handles_empty_and_failures(
    scheduler: SchedulerClient,
) -> None:
    scheduler.bridge_clients.clear()
    await scheduler.trigger_all_profiles_sync(source="api")

    good = FakeBridge()
    bad = FakeBridge()
    bad.sync_error = RuntimeError("bad")
    scheduler.bridge_clients.update(
        default=cast(BridgeClient, good),
        broken=cast(BridgeClient, bad),
    )

    with pytest.raises(ExceptionGroup, match="profile sync triggers failed"):
        await scheduler.trigger_all_profiles_sync(source="api")
    assert good.sync_calls


@pytest.mark.asyncio
async def test_status_metrics_and_lookup_error(
    scheduler: SchedulerClient,
) -> None:
    scheduler.failed_profile_errors["default"] = "bad config"
    status = await scheduler.get_status()
    assert status["default"]["status"]["initialization_error"] == "bad config"

    metrics = await scheduler.get_runtime_metrics()
    assert metrics["profile_count"] == 1
    assert metrics["bridge_count"] == 1
    assert metrics["queue_depth"] == 0

    with pytest.raises(ProfileNotFoundError):
        scheduler.get_profiles_for_source_provider("missing")


@pytest.mark.asyncio
async def test_database_sync_raises_backup_failures(
    scheduler: SchedulerClient,
) -> None:
    bridge = scheduler.bridge_clients["default"]
    assert isinstance(bridge, FakeBridge)
    bridge.backup_error = RuntimeError("backup boom")

    with pytest.raises(ExceptionGroup, match="daily profile backups failed"):
        await scheduler.trigger_database_sync(source="test")


@pytest.mark.asyncio
async def test_database_sync_timeout_is_propagated(
    scheduler: SchedulerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_sync_db() -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(scheduler.shared_animap_client, "sync_db", slow_sync_db)
    monkeypatch.setattr(sched_module, "_MAINTENANCE_TIMEOUT", 0.01)

    with pytest.raises(TimeoutError):
        await scheduler.trigger_database_sync(source="test")


@pytest.mark.asyncio
async def test_daily_loop_runs_due_database_sync_once(
    scheduler: SchedulerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    scheduler._running = True
    monkeypatch.setattr(scheduler, "_get_next_1am_utc", lambda now: now)

    async def trigger_database_sync(*, source: str) -> None:
        calls.append(source)
        scheduler.request_shutdown()

    monkeypatch.setattr(scheduler, "trigger_database_sync", trigger_database_sync)

    await scheduler._daily_db_sync_loop()

    assert calls == ["loop:daily_db"]


@pytest.mark.asyncio
async def test_daily_loop_logs_errors_then_continues(
    scheduler: SchedulerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    scheduler._running = True
    monkeypatch.setattr(scheduler, "_get_next_1am_utc", lambda now: now)

    async def trigger_database_sync(*, source: str) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError(source)

    async def fake_sleep(_seconds: float) -> None:
        scheduler.request_shutdown()

    monkeypatch.setattr(scheduler, "trigger_database_sync", trigger_database_sync)
    monkeypatch.setattr(sched_module.asyncio, "sleep", fake_sleep)

    await scheduler._daily_db_sync_loop()

    assert calls == 1


@pytest.mark.asyncio
async def test_reinitialize_and_remove_profile(
    scheduler: SchedulerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bridge = scheduler.bridge_clients["default"]
    new_bridge = FakeBridge()
    spawned: list[str] = []
    scheduler._running = True

    async def initialize_bridge(*_args, **_kwargs):
        return cast(BridgeClient, new_bridge)

    monkeypatch.setattr(scheduler, "_initialize_bridge_client", initialize_bridge)
    monkeypatch.setattr(
        scheduler,
        "_start_profile_producers",
        lambda profile_name: spawned.append(profile_name),
    )

    await scheduler.reinitialize_profile("default")

    assert isinstance(old_bridge, FakeBridge)
    assert old_bridge.closed is True
    assert scheduler.bridge_clients["default"] is new_bridge
    assert spawned == ["default"]

    with pytest.raises(ProfileNotFoundError):
        await scheduler.reinitialize_profile("missing")

    scheduler.failed_profile_errors["default"] = "bad"
    await scheduler.remove_profile("default")
    assert new_bridge.closed is True
    assert "default" not in scheduler.bridge_clients
    assert scheduler.failed_profile_errors == {}


@pytest.mark.asyncio
async def test_reinitialize_profile_wraps_initialization_errors(
    scheduler: SchedulerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def initialize_bridge(*_args, **_kwargs):
        raise RuntimeError("cannot init")

    monkeypatch.setattr(scheduler, "_initialize_bridge_client", initialize_bridge)

    with pytest.raises(sched_module.SchedulerUnavailableError, match="cannot init"):
        await scheduler.reinitialize_profile("default")
    assert scheduler.failed_profile_errors == {"default": "cannot init"}


@pytest.mark.asyncio
async def test_context_manager_and_wait_for_completion(
    scheduler: SchedulerClient,
) -> None:
    async with scheduler as entered:
        assert entered is scheduler

    assert await scheduler.wait_for_completion() is None
    scheduler._running = True
    waiter = asyncio.create_task(scheduler.wait_for_completion())
    await asyncio.sleep(0)
    scheduler.request_shutdown()
    await waiter
