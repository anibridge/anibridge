"""Tests for bridge orchestration."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from anibridge.provider.base import (
    Account,
    BackupArtifact,
    Capabilities,
    ChangeQuery,
    InboundRequest,
    InboundResult,
    Node,
    NodeChange,
    Page,
    Provider,
    Ref,
    Role,
    ScanItem,
    SupportsBackupExports,
    SupportsChangeFeed,
    SupportsInboundChanges,
)

import anibridge.app.core.bridge as bridge_module
from anibridge.app.config.settings import AnibridgeConfig, AnibridgeProfileConfig
from anibridge.app.core.bridge import BridgeClient
from anibridge.app.core.sync import ScanPlan, SyncRequest, SyncTrigger
from anibridge.app.core.sync.stats import SyncItem, SyncStats
from anibridge.app.logging import get_logger
from anibridge.app.models.db.sync_history import SyncOutcome


class _BridgeProvider(
    Provider,
    SupportsBackupExports,
    SupportsChangeFeed,
    SupportsInboundChanges,
):
    """Provider fake exposing all bridge-facing optional capabilities."""

    DISPLAY_NAME = "Bridge Provider"
    NAMESPACE = "provider"

    def __init__(
        self,
        *,
        namespace: str,
        role: Role,
        account_title: str = "Account",
    ) -> None:
        super().__init__(logger=get_logger(__name__), config={})
        self.NAMESPACE = namespace
        self.role = role
        self._account = Account(key=namespace, title=account_title)
        self.initialized = False
        self.closed = False
        self.close_error: Exception | None = None
        self.initialize_error: Exception | None = None
        self.backup: BackupArtifact | None = BackupArtifact(b"backup")
        self.backup_error: Exception | None = None
        self.change_pages: list[Page] = []
        self.inbound_result = InboundResult(
            matched=True,
            changes=(NodeChange(ref=Ref.anchor("inbound")),),
        )
        self.inbound_error: Exception | None = None
        self.inbound_requests: list[InboundRequest] = []

    def account(self) -> Account | None:
        return self._account

    def capabilities(self) -> Capabilities:
        return Capabilities(roles=frozenset({self.role}))

    async def initialize(self) -> None:
        if self.initialize_error is not None:
            raise self.initialize_error
        self.initialized = True

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    async def export_backup(self) -> BackupArtifact | None:
        if self.backup_error is not None:
            raise self.backup_error
        return self.backup

    async def poll_changes(self, query: ChangeQuery) -> Page:
        if self.change_pages:
            return self.change_pages.pop(0)
        return Page(items=(), cursor=None)

    async def parse_inbound(self, request: InboundRequest) -> InboundResult:
        self.inbound_requests.append(request)
        if self.inbound_error is not None:
            raise self.inbound_error
        return self.inbound_result


class _PlainProvider(Provider):
    """Provider fake with no optional bridge capabilities."""

    DISPLAY_NAME = "Plain"
    NAMESPACE = "plain"

    def account(self) -> Account | None:
        return None


class _FakeSyncClient:
    """SyncClient fake used to drive BridgeClient orchestration branches."""

    instances: ClassVar[list[_FakeSyncClient]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.scan_items = (
            ScanItem(node=Node(ref=Ref.anchor("a"), kind="anime")),
            ScanItem(node=Node(ref=Ref.anchor("b"), kind="anime")),
        )
        self.pages = [
            Page(items=(self.scan_items[0],), cursor="next", total=2),
            Page(items=(self.scan_items[1],), cursor=None, total=2),
        ]
        self.processed: list[tuple[ScanItem, ...]] = []
        self.process_error = False
        self.cleared = False
        self.flushed = False
        self.sync_stats = SyncStats()
        type(self).instances.append(self)

    async def scan_source_pages(self, *, scan: ScanPlan, page_size: int):
        self.scan = scan
        self.page_size = page_size
        for page in self.pages:
            yield page

    async def process_page(self, items):
        self.processed.append(tuple(items))
        if self.process_error:
            raise RuntimeError("process failed")

    def flush_failure_history_cleanup(self) -> None:
        self.flushed = True

    async def clear_cache(self) -> None:
        self.cleared = True


def _config(
    tmp_path: Path,
    **overrides: Any,
) -> tuple[AnibridgeConfig, AnibridgeProfileConfig]:
    profile = AnibridgeProfileConfig(
        source_provider="source",
        target_provider="target",
        backup_retention_days=overrides.pop("backup_retention_days", 30),
        full_scan=overrides.pop("full_scan", False),
        destructive_sync=overrides.pop("destructive_sync", False),
        dry_run=overrides.pop("dry_run", False),
        **overrides,
    )
    global_config = AnibridgeConfig(profiles={"default": profile})
    global_config.__dict__["data_path"] = tmp_path
    return global_config, profile


def _bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_db_factory,
    *,
    source: Provider | None = None,
    target: Provider | None = None,
    **profile_overrides: Any,
) -> BridgeClient:
    db_instance = sqlite_db_factory()
    monkeypatch.setattr(bridge_module, "db", lambda: db_instance)
    source = source or _BridgeProvider(namespace="source", role=Role.SOURCE)
    target = target or _BridgeProvider(namespace="target", role=Role.TARGET)
    monkeypatch.setattr(
        bridge_module,
        "build_profile_providers",
        lambda _profile, _config: {Role.SOURCE: source, Role.TARGET: target},
    )
    global_config, profile = _config(tmp_path, **profile_overrides)
    return BridgeClient(
        profile_name="default",
        profile_config=profile,
        global_config=global_config,
        shared_animap_client=cast(Any, object()),
    )


@pytest.mark.asyncio
async def test_bridge_initialize_close_and_backup_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_db_factory,
) -> None:
    """Bridge initialization should initialize providers, write backups, and close."""
    old_backup_dir = tmp_path / "backups" / "default"
    old_backup_dir.mkdir(parents=True)
    old_backup = old_backup_dir / "anibridge_default_target_old.json"
    old_backup.write_bytes(b"old")
    old_time = (datetime.now(UTC) - timedelta(days=10)).timestamp()
    old_backup.touch()
    old_backup.chmod(0o600)
    os.utime(old_backup, (old_time, old_time))

    bridge = _bridge(
        tmp_path,
        monkeypatch,
        sqlite_db_factory,
        backup_retention_days=1,
    )

    await bridge.initialize()
    async with bridge as entered:
        assert entered is bridge
    source_provider = cast(_BridgeProvider, bridge.source_provider)
    target_provider = cast(_BridgeProvider, bridge.target_provider)
    assert source_provider.initialized is True
    assert target_provider.initialized is True
    assert source_provider.closed is True
    assert target_provider.closed is True
    backups = sorted(old_backup_dir.glob("anibridge_default_target_*.json"))
    assert len(backups) == 1
    assert backups[0] != old_backup
    assert backups[0].read_bytes() == b"backup"


@pytest.mark.asyncio
async def test_bridge_initialize_surfaces_provider_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_db_factory,
) -> None:
    """Source and target initialization failures should propagate."""
    source = _BridgeProvider(namespace="source", role=Role.SOURCE)
    source.initialize_error = RuntimeError("source boom")
    bridge = _bridge(
        tmp_path,
        monkeypatch,
        sqlite_db_factory,
        source=source,
    )
    with pytest.raises(RuntimeError, match="source boom"):
        await bridge.initialize()

    target = _BridgeProvider(namespace="target", role=Role.TARGET)
    target.initialize_error = RuntimeError("target boom")
    bridge = _bridge(
        tmp_path,
        monkeypatch,
        sqlite_db_factory,
        target=target,
    )
    with pytest.raises(RuntimeError, match="target boom"):
        await bridge.initialize()


@pytest.mark.asyncio
async def test_bridge_backup_skips_disabled_empty_unsupported_and_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_db_factory,
) -> None:
    """Backup helper should tolerate unsupported and failed backups."""
    bridge = _bridge(
        tmp_path,
        monkeypatch,
        sqlite_db_factory,
        target=_PlainProvider(logger=get_logger(__name__), config={}),
    )
    await bridge._backup_target()

    target = _BridgeProvider(namespace="target", role=Role.TARGET)
    target.backup = None
    bridge = _bridge(tmp_path, monkeypatch, sqlite_db_factory, target=target)
    await bridge._backup_target()

    target.backup = BackupArtifact(b"")
    await bridge._backup_target()

    target.backup_error = RuntimeError("backup boom")
    await bridge._backup_target()

    bridge = _bridge(
        tmp_path,
        monkeypatch,
        sqlite_db_factory,
        backup_retention_days=-1,
    )
    await bridge._backup_target()


@pytest.mark.asyncio
async def test_bridge_sync_stream_skip_and_failure_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_db_factory,
) -> None:
    """Sync should drive stream, no-change, and failure branches."""
    monkeypatch.setattr(bridge_module, "SyncClient", _FakeSyncClient)
    monkeypatch.setattr(bridge_module, "release_memory", lambda: None)
    _FakeSyncClient.instances.clear()

    bridge = _bridge(tmp_path, monkeypatch, sqlite_db_factory)
    await bridge.sync(SyncRequest(trigger=SyncTrigger.MANUAL))
    stream_client = _FakeSyncClient.instances[-1]
    assert [len(items) for items in stream_client.processed] == [1, 1]
    assert stream_client.flushed is True
    assert stream_client.cleared is True
    assert bridge.last_synced is not None

    bridge = _bridge(tmp_path, monkeypatch, sqlite_db_factory)
    await bridge.sync(SyncRequest(trigger=SyncTrigger.WEBHOOK))
    skipped_client = _FakeSyncClient.instances[-1]
    assert skipped_client.processed == []

    original_process = _FakeSyncClient.process_page

    async def _boom(self, items):
        await original_process(self, items)
        raise RuntimeError("page failed")

    monkeypatch.setattr(_FakeSyncClient, "process_page", _boom)
    bridge = _bridge(tmp_path, monkeypatch, sqlite_db_factory)
    previous_last_synced = bridge.last_synced
    with pytest.raises(RuntimeError, match="page failed"):
        await bridge.sync(SyncRequest(trigger=SyncTrigger.MANUAL))
    assert _FakeSyncClient.instances[-1].cleared is True
    assert bridge.last_synced == previous_last_synced


@pytest.mark.asyncio
async def test_bridge_source_scan_poll_and_webhook_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_db_factory,
) -> None:
    """Requests should translate to source scans, change-feed refs, and webhooks."""
    source = _BridgeProvider(namespace="source", role=Role.SOURCE)
    source.change_pages = [
        Page(items=(NodeChange(ref=Ref.anchor("a")),), cursor="c1"),
        Page(items=(NodeChange(key="b"), NodeChange(ref=Ref.anchor("a"))), cursor=None),
    ]
    bridge = _bridge(tmp_path, monkeypatch, sqlite_db_factory, source=source)

    manual = await bridge._scan_plan_for(SyncRequest())
    assert manual == ScanPlan(
        trigger=SyncTrigger.MANUAL,
        source_refs=None,
        require_user_data=True,
    )

    targeted = await bridge._scan_plan_for(
        SyncRequest(trigger=SyncTrigger.MANUAL, source_refs=(Ref.anchor("x"),))
    )
    assert targeted is not None
    assert targeted.source_refs == (Ref.anchor("x"),)
    assert targeted.require_user_data is False

    poll = await bridge._scan_plan_for(
        SyncRequest(trigger=SyncTrigger.POLL, source_refs=(Ref.anchor("a"),))
    )
    assert poll is not None
    assert poll.source_refs == (Ref.anchor("a"), Ref.anchor("b"))
    assert poll.from_change_feed is True

    source.change_pages = [Page(items=(NodeChange(ref=Ref.anchor("changed")),))]
    coalesced_poll = await bridge._scan_plan_for(
        SyncRequest(
            trigger=SyncTrigger.POLL,
            source_refs=(Ref.anchor("queued"),),
            full_scan_on_poll_fallback=True,
        )
    )
    assert coalesced_poll is not None
    assert coalesced_poll.source_refs == (Ref.anchor("changed"), Ref.anchor("queued"))
    assert coalesced_poll.from_change_feed is True

    webhook = await bridge._scan_plan_for(
        SyncRequest(trigger=SyncTrigger.WEBHOOK, source_refs=(Ref.anchor("w"),))
    )
    assert webhook is not None
    assert webhook.source_refs == (Ref.anchor("w"),)
    assert await bridge._scan_plan_for(SyncRequest(trigger=SyncTrigger.WEBHOOK)) is None

    matched, refs = await bridge.parse_webhook(cast(Any, _RequestStub()))
    assert matched is True
    assert refs == (Ref.anchor("inbound"),)
    assert source.inbound_requests[0].query == {"a": ("1", "2"), "b": ("3",)}


@pytest.mark.asyncio
async def test_bridge_poll_and_webhook_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_db_factory,
) -> None:
    """Unsupported poll/webhook providers and parser errors should be non-fatal."""
    bridge = _bridge(
        tmp_path,
        monkeypatch,
        sqlite_db_factory,
        source=_PlainProvider(logger=get_logger(__name__), config={}),
        full_scan=True,
    )
    poll = await bridge._scan_plan_for(SyncRequest(trigger=SyncTrigger.POLL))
    assert poll is not None
    assert poll.require_user_data is False

    fallback_poll = await bridge._scan_plan_for(
        SyncRequest(
            trigger=SyncTrigger.POLL,
            source_refs=(Ref.anchor("queued"),),
            full_scan_on_poll_fallback=True,
        )
    )
    assert fallback_poll is not None
    assert fallback_poll.source_refs is None
    assert fallback_poll.require_user_data is False
    assert await bridge.parse_webhook(cast(Any, object())) == (False, None)

    source = _BridgeProvider(namespace="source", role=Role.SOURCE)
    source.inbound_result = InboundResult(matched=False)
    bridge = _bridge(tmp_path, monkeypatch, sqlite_db_factory, source=source)
    assert await bridge.parse_webhook(cast(Any, _RequestStub())) == (False, None)

    source.inbound_error = RuntimeError("bad payload")
    assert await bridge.parse_webhook(cast(Any, _RequestStub())) == (False, None)


def test_bridge_housekeeping_keys_and_change_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_db_factory,
) -> None:
    """Housekeeping and change-ref helpers should round-trip values."""
    bridge = _bridge(tmp_path, monkeypatch, sqlite_db_factory)
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)

    assert bridge._get_last_synced() is None
    bridge._set_last_synced(timestamp)
    assert bridge._get_last_synced() == timestamp

    assert bridge._get_change_cursor() is None
    bridge._set_change_cursor("cursor")
    assert bridge._get_change_cursor() == "cursor"

    refs = BridgeClient._change_refs(
        [
            NodeChange(ref=Ref.anchor("a")),
            NodeChange(ref=Ref.anchor("a")),
            NodeChange(key="b"),
            NodeChange(),
        ]
    )
    assert refs == (Ref.anchor("a"), Ref.anchor("b"))


@pytest.mark.asyncio
async def test_bridge_error_and_fallback_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sqlite_db_factory,
) -> None:
    """Close, backup write, sync failure, and poll-empty branches should recover."""
    target = _BridgeProvider(namespace="target", role=Role.TARGET)
    target.close_error = RuntimeError("close failed")
    bridge = _bridge(tmp_path, monkeypatch, sqlite_db_factory, target=target)
    await bridge.close()

    blocked_path = tmp_path / "blocked"
    blocked_path.write_text("file", encoding="utf-8")
    bridge.global_config.__dict__["data_path"] = blocked_path
    await bridge._backup_target()

    source = _BridgeProvider(namespace="source", role=Role.SOURCE)
    bridge = _bridge(tmp_path, monkeypatch, sqlite_db_factory, source=source)
    assert await bridge._scan_plan_for(SyncRequest(trigger=SyncTrigger.POLL)) is None

    source.change_pages = [Page(items=(), cursor="same")]
    bridge._set_change_cursor("same")
    assert await bridge._poll_source_refs() == ()
    assert "returned an unchanged change-feed cursor" not in caplog.text

    monkeypatch.setattr(bridge_module, "SyncClient", _FakeSyncClient)
    monkeypatch.setattr(bridge_module, "release_memory", lambda: None)
    original_scan_pages = _FakeSyncClient.scan_source_pages

    async def _scan_boom(
        self: _FakeSyncClient,
        *,
        scan: ScanPlan,
        page_size: int,
    ):
        async for page in original_scan_pages(self, scan=scan, page_size=page_size):
            yield page
        self.sync_stats.track_item(
            SyncItem(
                namespace="source",
                ref=Ref.anchor("uncovered"),
                repr="Uncovered",
            ),
            SyncOutcome.NOT_FOUND,
        )
        raise RuntimeError("sync failed")

    monkeypatch.setattr(_FakeSyncClient, "scan_source_pages", _scan_boom)
    bridge = _bridge(tmp_path, monkeypatch, sqlite_db_factory)
    with pytest.raises(RuntimeError, match="sync failed"):
        await bridge.sync(SyncRequest(trigger=SyncTrigger.MANUAL))
    assert _FakeSyncClient.instances[-1].cleared is True


class _RequestStub:
    method = "POST"
    headers: ClassVar[dict[str, str]] = {"x-test": "yes"}
    query_params: ClassVar[dict[str, Any]] = {"a": ["1", "2"], "b": 3}
    url = SimpleNamespace(path="/webhook/source")

    async def body(self) -> bytes:
        return b"payload"
