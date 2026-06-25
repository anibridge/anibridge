"""Unit tests for provider-contract sync history persistence helpers."""

import pytest
from anibridge.provider.base import Node, Progress, Record, RecordField, Ref

from anibridge.app.core.sync.history import SyncHistoryManager
from anibridge.app.core.sync.stats import RecordSnapshot
from anibridge.app.models.db.sync_history import SyncHistory, SyncOutcome


@pytest.fixture
def history_manager(sqlite_db_factory) -> SyncHistoryManager:
    """Create a sync history manager bound to an in-memory database."""
    return SyncHistoryManager(
        profile_name="profile",
        source_namespace="source",
        target_namespace="target",
        db_factory=sqlite_db_factory,
    )


def _node(key: str = "src1") -> Node:
    return Node(ref=Ref.anchor(key), kind="anime", title=f"Source {key}")


def _record(key: str = "src1") -> Record:
    return Record(
        ref=Ref.anchor(key),
        surface="user_state",
        values={RecordField.PROGRESS: Progress(current=1, total=12)},
    )


@pytest.mark.asyncio
async def test_create_sync_history_skips_skipped_rows(
    history_manager: SyncHistoryManager,
    sqlite_db_factory,
) -> None:
    """Skipped outcomes should not persist rows."""
    await history_manager.create_sync_history(
        source_node=_node(),
        source_record=_record(),
        target_ref=None,
        snapshots=(None, None),
        outcome=SyncOutcome.SKIPPED,
    )

    with sqlite_db_factory() as ctx:
        assert ctx.session.query(SyncHistory).count() == 0


@pytest.mark.asyncio
async def test_create_sync_history_updates_existing_failure_record(
    history_manager: SyncHistoryManager,
    sqlite_db_factory,
) -> None:
    """Repeated failure rows should update, not duplicate, the stored failure."""
    await history_manager.create_sync_history(
        source_node=_node(),
        source_record=_record(),
        target_ref=Ref.anchor("tgt1"),
        snapshots=(None, None),
        outcome=SyncOutcome.FAILED,
        error_message="old",
    )
    await history_manager.create_sync_history(
        source_node=_node(),
        source_record=_record(),
        target_ref=Ref.anchor("tgt1"),
        snapshots=(None, RecordSnapshot.from_record(_record("tgt1"))),
        outcome=SyncOutcome.FAILED,
        error_message="new",
        info={"source": "retry"},
    )

    with sqlite_db_factory() as ctx:
        rows = ctx.session.query(SyncHistory).all()
        assert len(rows) == 1
        assert rows[0].error_message == "new"
        assert rows[0].source_record_surface == "user_state"
        assert rows[0].target_record_surface == "user_state"
        assert rows[0].info["source"] == "retry"
        assert "source_record_surface" not in rows[0].info
        assert "target_record_surface" not in rows[0].info


@pytest.mark.asyncio
async def test_successful_sync_cleans_matching_failure_rows(
    history_manager: SyncHistoryManager,
    sqlite_db_factory,
) -> None:
    """Successful syncs should remove stale failed and not-found rows for the ref."""
    await history_manager.create_sync_history(
        source_node=_node(),
        source_record=_record(),
        target_ref=Ref.anchor("tgt1"),
        snapshots=(None, None),
        outcome=SyncOutcome.FAILED,
    )
    await history_manager.create_sync_history(
        source_node=_node(),
        source_record=_record(),
        target_ref=None,
        snapshots=(None, None),
        outcome=SyncOutcome.NOT_FOUND,
    )
    await history_manager.create_sync_history(
        source_node=_node(),
        source_record=_record(),
        target_ref=Ref.anchor("tgt1"),
        snapshots=(None, RecordSnapshot.from_record(_record("tgt1"))),
        outcome=SyncOutcome.SYNCED,
    )
    history_manager.flush_failure_history_cleanup()

    with sqlite_db_factory() as ctx:
        rows = ctx.session.query(SyncHistory).all()
        assert len(rows) == 1
        assert rows[0].outcome == SyncOutcome.SYNCED
