"""Tests for the sync history service."""

from types import SimpleNamespace
from typing import Any

import pytest
from anibridge.provider.base import (
    Artwork,
    FacetName,
    Node,
    NodeQuery,
    Page,
    Progress,
    Record,
    RecordField,
    Ref,
    SupportsNodeReads,
)

import anibridge.app.web.services.history_service as history_service_module
from anibridge.app.core.sync import SyncRequest
from anibridge.app.core.sync.history import to_builtins
from anibridge.app.core.sync.stats import RecordSnapshot
from anibridge.app.exceptions import HistoryItemNotFoundError, HistoryPermissionError
from anibridge.app.models.db.pin import Pin
from anibridge.app.models.db.sync_history import SyncHistory, SyncOutcome
from anibridge.app.web.services.history_service import (
    HistoryService,
    get_history_service,
)


class FakeNodeProvider(SupportsNodeReads):
    """Provider double that returns node metadata by ref."""

    def __init__(self, namespace: str) -> None:
        self.NAMESPACE = namespace

    async def fetch_nodes(self, query: NodeQuery) -> Page[Node]:
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
        "schedule_task",
        lambda coro, *, name: coro.close(),
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
            ctx.session.query(SyncHistory).delete()
            ctx.session.query(Pin).delete()
            ctx.session.commit()

        payload = {
            "profile_name": "profile",
            "source_namespace": "source",
            "source_ref": _ref_payload("src1"),
            "target_namespace": "target",
            "target_ref": _ref_payload("tgt1"),
            "source_record_surface": "source_state",
            "target_record_surface": "target_state",
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
        }
        payload.update(overrides)
        row = SyncHistory(**payload)
        ctx.session.add(row)
        if pin and payload.get("target_ref"):
            ctx.session.add(
                Pin(
                    profile_name=payload["profile_name"],
                    target_namespace=payload["target_namespace"],
                    target_ref=payload["target_ref"],
                    fields=["status"],
                )
            )
        ctx.session.commit()
        return row.id


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

    assert page.latest_id == row_id
    assert page.has_more is False
    assert page.stats == {SyncOutcome.SYNCED.value: 1}
    item = page.items[0]
    assert item.source_media is not None
    assert item.source_media.title == "source:src1"
    assert item.target_media is not None
    assert item.target_media.poster_url == "https://img.test/tgt1.jpg"
    assert item.before_state is not None
    assert item.after_state is not None
    assert item.source_record_surface == "source_state"
    assert item.target_record_surface == "target_state"
    assert item.pinned_fields == ["status"]
    assert item.info == {"source": "test-seed"}


@pytest.mark.asyncio
async def test_history_service_pin_fields_prefer_exact_refs(history_env):
    """History pin display should prefer exact refs over anchor pins."""
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
                    target_ref=_ref_payload("tgt1"),
                    fields=["rating"],
                ),
                Pin(
                    profile_name="profile",
                    target_namespace="target",
                    target_ref=episode_1,
                    fields=["notes"],
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

    fields_by_episode = {
        item.target_ref.path[0].value: item.pinned_fields
        for item in page.items
        if item.target_ref is not None
    }
    assert fields_by_episode == {1: ["notes"], 2: ["rating"]}


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

    assert [item.id for item in failed_page.items] == [row2]
    assert failed_page.items[0].error_message == "boom"
    assert [item.id for item in after_page.items] == [row2]


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
async def test_history_service_delete_item_removes_row(history_env):
    """delete_item should remove only the requested profile row."""
    row_id = _seed_history_row()
    service = HistoryService()

    await service.delete_item("profile", row_id)

    with pytest.raises(HistoryItemNotFoundError):
        await service.delete_item("profile", row_id)


@pytest.mark.asyncio
async def test_history_service_retry_item_targets_source_ref(
    history_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_item should resubmit failed rows as targeted provider scans."""
    row_id = _seed_history_row(outcome=SyncOutcome.FAILED)
    scheduled: list[tuple[Any, str]] = []

    async def fake_trigger(profile: str, *, request: SyncRequest, source: str) -> None:
        history_env.scheduler.calls.append((profile, request, source))

    history_env.scheduler.trigger_profile_sync = fake_trigger
    monkeypatch.setattr(
        history_service_module,
        "schedule_task",
        lambda coro, *, name: scheduled.append((coro, name)),
    )

    await HistoryService().retry_item("profile", row_id)

    coro, name = scheduled[0]
    assert name == f"retry_history_item:profile:{row_id}"
    await coro
    profile, request, source = history_env.scheduler.calls[0]
    assert profile == "profile"
    assert source == "history:retry_item"
    assert request.source_refs == (Ref.anchor("src1"),)


@pytest.mark.asyncio
async def test_history_service_undo_item_schedules_record_undo(
    history_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """undo_item should submit restorable target states through SyncRequest."""
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
        "schedule_task",
        lambda coro, *, name: scheduled.append((coro, name)),
    )

    await HistoryService().undo_item("profile", row_id)

    coro, name = scheduled[0]
    assert name == f"undo_history_item:profile:{row_id}"
    await coro
    profile, request, source = history_env.scheduler.calls[0]
    assert profile == "profile"
    assert source == "history:undo_item"
    assert request.source_refs == ()
    undo = request.record_undos[0]
    assert undo.source_ref == Ref.anchor("src1")
    assert undo.target_ref == Ref.anchor("tgt1")
    assert undo.before is not None
    assert undo.before.values_for_restore() == {
        RecordField.PROGRESS: Progress(current=0, total=12)
    }


@pytest.mark.asyncio
async def test_history_service_retry_item_rejects_synced_rows(history_env) -> None:
    """Retry should remain limited to failed and not-found rows."""
    row_id = _seed_history_row(outcome=SyncOutcome.SYNCED)

    with pytest.raises(HistoryPermissionError, match="failed or not found"):
        await HistoryService().retry_item("profile", row_id)


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
    assert len(page.items) == 1
    assert page.items[0].target_ref is not None
    assert page.items[0].target_ref.key == "tgt2"


@pytest.mark.asyncio
async def test_history_service_build_history_items_short_circuits(history_env):
    """Empty history row batches should avoid provider work."""
    assert await HistoryService()._build_history_items("profile", []) == []


def test_get_history_service_returns_singleton() -> None:
    """The cached service factory should return a singleton."""
    assert get_history_service() is get_history_service()
