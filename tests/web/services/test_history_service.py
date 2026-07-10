"""Tests for the sync history service."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anibridge.provider.base import (
    Artwork,
    Event,
    FacetName,
    Node,
    NodeQuery,
    Page,
    Progress,
    Query,
    Record,
    RecordField,
    Ref,
    SupportsReads,
)

import anibridge.app.web.services.history_service as history_service_module
from anibridge.app.core.sync import SyncRequest
from anibridge.app.core.sync.history import to_builtins
from anibridge.app.core.sync.stats import RecordSnapshot
from anibridge.app.exceptions import (
    HistoryItemNotFoundError,
    HistoryPermissionError,
    ProfileNotFoundError,
)
from anibridge.app.models.db.pin import Pin
from anibridge.app.models.db.sync_history import (
    SyncHistoryGroup,
    SyncHistoryOperation,
    SyncHistoryRun,
    SyncOperationAction,
    SyncOutcome,
    SyncResourceKind,
)
from anibridge.app.web.services.history_service import (
    HistoryService,
    get_history_service,
)


class FakeNodeProvider(SupportsReads):
    """Provider double that returns node metadata by ref."""

    def __init__(self, namespace: str) -> None:
        self.NAMESPACE = namespace

    async def fetch(self, query: Query) -> Page[Node | Record | Event]:
        assert isinstance(query, NodeQuery)
        return Page(
            items=tuple(
                Node(
                    ref=ref,
                    kind="anime",
                    title=f"{self.NAMESPACE}:{ref.key}",
                    url=f"https://example.test/{self.NAMESPACE}/{ref.key}",
                    labels=(self.NAMESPACE,),
                    facets={
                        FacetName.ARTWORK: Artwork(
                            images={"poster": f"https://img.test/{ref.key}.jpg"}
                        )
                    },
                )
                for ref in query.refs
            )
        )


class FakeScheduler:
    """Scheduler double that records targeted retry requests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, SyncRequest, str]] = []

    async def trigger_profile_sync(
        self,
        profile: str,
        *,
        request: SyncRequest,
        source: str,
    ) -> None:
        self.calls.append((profile, request, source))


@pytest.fixture()
def history_env(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_db_factory,
):
    """Patch history service dependencies for isolated DB-backed tests."""
    in_memory_db_factory(monkeypatch, history_service_module)
    bridge = SimpleNamespace(
        source_provider=FakeNodeProvider("source"),
        target_provider=FakeNodeProvider("target"),
    )
    monkeypatch.setattr(history_service_module, "get_bridge", lambda _profile: bridge)
    scheduler = FakeScheduler()
    monkeypatch.setattr(
        history_service_module,
        "get_app_state",
        lambda: SimpleNamespace(scheduler=scheduler),
    )
    monkeypatch.setattr(
        history_service_module,
        "_background_tasks",
        SimpleNamespace(create=lambda coro, *, name: coro.close()),
    )
    return SimpleNamespace(bridge=bridge, scheduler=scheduler)


def _ref_payload(
    key: str,
    path: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {"key": key, "path": path or []}


def _snapshot_payload(key: str, progress: int) -> dict[str, object]:
    return to_builtins(
        RecordSnapshot.from_record(
            Record(
                ref=Ref.anchor(key),
                surface="target_state",
                values={RecordField.PROGRESS: Progress(current=progress, total=12)},
            )
        )
    )


def _seed_history_row(
    *,
    clear: bool = True,
    pin: bool = True,
    **overrides: Any,
) -> int:
    with history_service_module.db() as ctx:
        if clear:
            ctx.session.query(SyncHistoryOperation).delete()
            ctx.session.query(SyncHistoryGroup).delete()
            ctx.session.query(SyncHistoryRun).delete()
            ctx.session.query(Pin).delete()
            ctx.session.commit()

        payload = {
            "profile_name": "profile",
            "source_namespace": "source",
            "source_ref": _ref_payload("src1"),
            "target_namespace": "target",
            "target_ref": _ref_payload("tgt1"),
            "source_surface": "source_state",
            "target_surface": "target_state",
            "outcome": SyncOutcome.SYNCED,
            "before_state": {
                "ref": _ref_payload("tgt1"),
                "surface": "target_state",
                "values": {"progress": {"current": 0, "total": 12}},
            },
            "after_state": {
                "ref": _ref_payload("tgt1"),
                "surface": "target_state",
                "values": {"progress": {"current": 1, "total": 12}},
            },
            "info": {"source": "test-seed"},
            "error_message": None,
            "ephemeral": False,
        }
        payload.update(overrides)

        row_number = (
            ctx.session.query(SyncHistoryGroup).count()
            + ctx.session.query(SyncHistoryOperation).count()
            + 1
        )
        timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=row_number)
        run = SyncHistoryRun(
            profile_name=payload["profile_name"],
            source_namespace=payload["source_namespace"],
            target_namespace=payload["target_namespace"],
            outcome=payload["outcome"],
            info={},
            ephemeral=payload["ephemeral"],
            started_at=timestamp,
            completed_at=timestamp,
        )
        ctx.session.add(run)
        ctx.session.flush()
        source_ref = payload["source_ref"]
        target_ref = cast(dict[str, object] | None, payload.get("target_ref"))
        group = SyncHistoryGroup(
            run_id=run.id,
            profile_name=payload["profile_name"],
            source_namespace=payload["source_namespace"],
            source_parent_ref=_ref_payload(cast(str, source_ref["key"])),
            target_namespace=payload["target_namespace"],
            target_parent_ref=_ref_payload(cast(str, target_ref["key"]))
            if target_ref
            else None,
            outcome=payload["outcome"],
            operation_count=1,
            record_count=1,
            event_count=0,
            node_count=0,
            error_count=1
            if payload["outcome"] in (SyncOutcome.FAILED, SyncOutcome.NOT_FOUND)
            else 0,
            info=payload["info"],
            ephemeral=payload["ephemeral"],
            timestamp=timestamp,
        )
        ctx.session.add(group)
        ctx.session.flush()
        operation = SyncHistoryOperation(
            group_id=group.id,
            profile_name=payload["profile_name"],
            resource_kind=SyncResourceKind.RECORD,
            action=SyncOperationAction.UPSERT,
            source_namespace=payload["source_namespace"],
            source_ref=payload["source_ref"],
            target_namespace=payload["target_namespace"],
            target_ref=payload.get("target_ref"),
            source_surface=payload["source_surface"],
            target_surface=payload["target_surface"],
            outcome=payload["outcome"],
            before_state=payload["before_state"],
            after_state=payload["after_state"],
            info=payload["info"],
            error_message=payload["error_message"],
            ephemeral=payload["ephemeral"],
            timestamp=timestamp,
        )
        ctx.session.add(operation)
        if pin and payload.get("target_ref"):
            ctx.session.add(
                Pin(
                    profile_name=payload["profile_name"],
                    target_namespace=payload["target_namespace"],
                    target_parent_ref=_ref_payload(str(payload["target_ref"]["key"])),
                )
            )
        ctx.session.commit()
        return group.id


@pytest.mark.asyncio
async def test_history_service_get_page_enriches_metadata_and_pins(history_env):
    """History pages include provider metadata, snapshots, stats, and pins."""
    row_id = _seed_history_row()
    service = HistoryService()

    page = await service.get_page(
        profile="profile",
        limit=10,
        include_source_media=True,
        include_target_media=True,
        include_stats=True,
    )

    assert page.latest_group_id == row_id
    assert page.has_more is False
    assert page.stats == {SyncOutcome.SYNCED.value: 1}
    assert page.resource_stats == {SyncResourceKind.RECORD.value: 1}
    group = page.groups[0]
    operation = group.operations[0]
    assert group.source_media is not None
    assert group.source_media.title == "source:src1"
    assert group.target_media is not None
    assert group.target_media.poster_url == "https://img.test/tgt1.jpg"
    assert operation.before_state is not None
    assert operation.after_state is not None
    assert operation.source_surface == "source_state"
    assert operation.target_surface == "target_state"
    assert operation.pinned is True
    assert operation.info == {"source": "test-seed"}


@pytest.mark.asyncio
async def test_history_service_get_page_works_without_initialized_bridge(
    history_env,
    monkeypatch: pytest.MonkeyPatch,
):
    """Saved history remains readable when provider clients failed to initialize."""
    _seed_history_row(pin=False)
    monkeypatch.setattr(
        history_service_module,
        "get_bridge",
        lambda _profile: (_ for _ in ()).throw(ProfileNotFoundError("profile")),
    )
    monkeypatch.setattr(
        history_service_module,
        "get_config",
        lambda: SimpleNamespace(
            get_profile=lambda _profile: SimpleNamespace(
                source_provider="source",
                target_provider="target",
            )
        ),
    )

    page = await HistoryService().get_page("profile", include_stats=True)

    assert len(page.groups) == 1
    assert page.groups[0].source_media is None
    assert page.groups[0].target_media is None
    assert page.latest_group_id == page.groups[0].id
    assert page.stats == {SyncOutcome.SYNCED.value: 1}


@pytest.mark.asyncio
async def test_history_service_pins_cover_child_refs(history_env):
    """History pin display should mark child refs by target parent pin."""
    episode_1 = _ref_payload("tgt1", [{"axis": "episode", "value": 1}])
    episode_2 = _ref_payload("tgt1", [{"axis": "episode", "value": 2}])
    _seed_history_row(pin=False, target_ref=episode_1)
    _seed_history_row(clear=False, pin=False, target_ref=episode_2)
    with history_service_module.db() as ctx:
        ctx.session.add_all(
            [
                Pin(
                    profile_name="profile",
                    target_namespace="target",
                    target_parent_ref=_ref_payload("tgt1"),
                ),
            ]
        )
        ctx.session.commit()

    page = await HistoryService().get_page(
        profile="profile",
        limit=10,
        include_source_media=False,
        include_target_media=False,
    )

    pinned_by_episode = {
        operation.target_ref.path[0].value: operation.pinned
        for group in page.groups
        for operation in group.operations
        if operation.target_ref is not None
    }
    assert pinned_by_episode == {1: True, 2: True}


@pytest.mark.asyncio
async def test_history_service_pathful_pins_do_not_cover_siblings(history_env):
    """Exact child pins should not mark sibling provider refs as pinned."""
    episode_1 = _ref_payload("tgt1", [{"axis": "episode", "value": 1}])
    episode_2 = _ref_payload("tgt1", [{"axis": "episode", "value": 2}])
    _seed_history_row(pin=False, target_ref=episode_1)
    _seed_history_row(clear=False, pin=False, target_ref=episode_2)
    with history_service_module.db() as ctx:
        ctx.session.add(
            Pin(
                profile_name="profile",
                target_namespace="target",
                target_parent_ref=episode_1,
            )
        )
        ctx.session.commit()

    page = await HistoryService().get_page(
        profile="profile",
        limit=10,
        include_source_media=False,
        include_target_media=False,
    )

    pinned = {
        operation.target_ref.path[0].value: operation.pinned
        for group in page.groups
        for operation in group.operations
        if operation.target_ref is not None
    }
    assert pinned == {1: True, 2: False}


@pytest.mark.asyncio
async def test_history_service_get_page_paginates_and_filters(history_env):
    """Cursor pagination and outcome filters should apply to history rows."""
    row1 = _seed_history_row(target_ref=_ref_payload("tgt1"))
    row2 = _seed_history_row(
        clear=False,
        source_ref=_ref_payload("src2"),
        target_ref=_ref_payload("tgt2"),
        outcome=SyncOutcome.FAILED,
        error_message="boom",
    )
    service = HistoryService()

    failed_page = await service.get_page(
        profile="profile",
        limit=10,
        outcome=SyncOutcome.FAILED.value,
        include_source_media=False,
        include_target_media=False,
    )
    after_page = await service.get_page(
        profile="profile",
        limit=10,
        after_id=row1,
        include_source_media=False,
        include_target_media=False,
    )

    assert [group.id for group in failed_page.groups] == [row2]
    assert failed_page.groups[0].operations[0].error_message == "boom"
    assert [group.id for group in after_page.groups] == [row2]


@pytest.mark.asyncio
async def test_history_service_resource_stats_follow_provider_scope(history_env):
    """Resource stats should use the same source/target scope as outcome stats."""
    _seed_history_row(target_ref=_ref_payload("tgt1"))
    _seed_history_row(
        clear=False,
        source_ref=_ref_payload("other-src"),
        target_ref=_ref_payload("other-tgt"),
        source_namespace="other-source",
        target_namespace="other-target",
    )

    page = await HistoryService().get_page(
        profile="profile",
        limit=10,
        include_source_media=False,
        include_target_media=False,
        include_stats=True,
    )

    assert page.stats == {SyncOutcome.SYNCED.value: 1}
    assert page.resource_stats == {SyncResourceKind.RECORD.value: 1}


@pytest.mark.asyncio
async def test_history_service_get_latest_id_uses_provider_scope(history_env):
    """Latest-id lookups should honor profile/source/target filters."""
    row1 = _seed_history_row(target_ref=_ref_payload("tgt1"))
    row2 = _seed_history_row(
        clear=False,
        source_ref=_ref_payload("src2"),
        target_ref=_ref_payload("tgt2"),
        outcome=SyncOutcome.FAILED,
    )
    service = HistoryService()

    assert await service.get_latest_id("profile") == row2
    assert (
        await service.get_latest_id("profile", outcome=SyncOutcome.SYNCED.value) == row1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit must be >= 1"),
        ({"limit": 251}, "limit must be <= 250"),
        ({"before_id": 1, "after_id": 2}, "mutually exclusive"),
    ],
)
async def test_history_service_get_page_validates_inputs(
    history_env,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    """Invalid page parameters should raise clear ValueError messages."""
    service = HistoryService()

    with pytest.raises(ValueError, match=message):
        await service.get_page(profile="profile", **kwargs)


@pytest.mark.asyncio
async def test_history_service_delete_group_removes_row(history_env):
    """delete_group should remove only the requested profile group."""
    row_id = _seed_history_row()
    service = HistoryService()

    await service.delete_group("profile", row_id)

    with pytest.raises(HistoryItemNotFoundError):
        await service.delete_group("profile", row_id)


@pytest.mark.asyncio
async def test_history_service_retry_group_targets_source_ref(
    history_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_group should resubmit failed groups as targeted provider scans."""
    row_id = _seed_history_row(outcome=SyncOutcome.FAILED)
    scheduled: list[tuple[Any, str]] = []

    async def fake_trigger(profile: str, *, request: SyncRequest, source: str) -> None:
        history_env.scheduler.calls.append((profile, request, source))

    history_env.scheduler.trigger_profile_sync = fake_trigger
    monkeypatch.setattr(
        history_service_module,
        "_background_tasks",
        SimpleNamespace(create=lambda coro, *, name: scheduled.append((coro, name))),
    )

    await HistoryService().retry_group("profile", row_id)

    coro, name = scheduled[0]
    assert name == f"retry_history_group:profile:{row_id}"
    await coro
    profile, request, source = history_env.scheduler.calls[0]
    assert profile == "profile"
    assert source == "history:retry_group"
    assert request.source_refs == (Ref.anchor("src1"),)


@pytest.mark.asyncio
async def test_history_service_undo_operation_schedules_record_undo(
    history_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """undo_operation should submit restorable target states through SyncRequest."""
    row_id = _seed_history_row(
        before_state=_snapshot_payload("tgt1", 0),
        after_state=_snapshot_payload("tgt1", 1),
    )
    scheduled: list[tuple[Any, str]] = []

    async def fake_trigger(profile: str, *, request: SyncRequest, source: str) -> None:
        history_env.scheduler.calls.append((profile, request, source))

    history_env.scheduler.trigger_profile_sync = fake_trigger
    monkeypatch.setattr(
        history_service_module,
        "_background_tasks",
        SimpleNamespace(create=lambda coro, *, name: scheduled.append((coro, name))),
    )

    await HistoryService().undo_operation("profile", row_id)

    coro, name = scheduled[0]
    assert name == f"undo_history_operation:profile:{row_id}"
    await coro
    profile, request, source = history_env.scheduler.calls[0]
    assert profile == "profile"
    assert source == "history:undo_operation"
    assert request.source_refs == ()
    undo = request.record_undos[0]
    assert undo.source_ref == Ref.anchor("src1")
    assert undo.target_ref == Ref.anchor("tgt1")
    assert undo.before is not None
    assert undo.before.values_for_restore() == {
        RecordField.PROGRESS: Progress(current=0, total=12)
    }


@pytest.mark.asyncio
async def test_history_service_retry_group_rejects_synced_rows(history_env) -> None:
    """Retry should remain limited to failed and not-found rows."""
    row_id = _seed_history_row(outcome=SyncOutcome.SYNCED)

    with pytest.raises(HistoryPermissionError, match="failed or not found"):
        await HistoryService().retry_group("profile", row_id)


@pytest.mark.asyncio
async def test_history_service_purge_ephemeral_items(history_env):
    """Purge should delete only dry-run history rows."""
    _seed_history_row(ephemeral=True)
    _seed_history_row(
        clear=False,
        source_ref=_ref_payload("src2"),
        target_ref=_ref_payload("tgt2"),
        ephemeral=False,
    )
    service = HistoryService()

    assert await service.purge_ephemeral_items() == 1

    page = await service.get_page(
        profile="profile",
        limit=10,
        include_source_media=False,
        include_target_media=False,
    )
    assert len(page.groups) == 1
    operation = page.groups[0].operations[0]
    assert operation.target_ref is not None
    assert operation.target_ref.key == "tgt2"


@pytest.mark.asyncio
async def test_history_service_build_history_groups_short_circuits(history_env):
    """Empty history row batches should avoid provider work."""
    assert await HistoryService()._build_history_groups("profile", []) == []


def test_get_history_service_returns_singleton() -> None:
    """The cached service factory should return a singleton."""
    assert get_history_service() is get_history_service()
