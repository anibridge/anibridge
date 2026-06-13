"""Tests for global scheduler coordination primitives."""

import asyncio

import pytest

from anibridge.app.core.sched.coord import GlobalSyncCoordinator


@pytest.mark.asyncio
async def test_profile_slots_wait_for_pending_maintenance() -> None:
    coordinator = GlobalSyncCoordinator()
    await coordinator.acquire_profile_slot("default")
    assert coordinator.get_metrics()["active_profile_syncs"] == 1

    work_started = asyncio.Event()

    async def work() -> None:
        work_started.set()

    maintenance = asyncio.create_task(coordinator.run_maintenance(work))
    await asyncio.sleep(0)
    metrics = coordinator.get_metrics()
    assert metrics["maintenance_waiting"] == 1
    assert metrics["maintenance_active"] is False

    slot_waiter = asyncio.create_task(coordinator.acquire_profile_slot("other"))
    await asyncio.sleep(0)
    assert slot_waiter.done() is False

    coordinator.release_profile_slot("default")
    await maintenance
    await asyncio.wait_for(work_started.wait(), timeout=1)
    await slot_waiter
    assert coordinator.get_metrics()["active_profile_syncs"] == 1

    coordinator.release_profile_slot("other")
    coordinator.release_profile_slot("extra")
    assert coordinator.get_metrics()["active_profile_syncs"] == 0


@pytest.mark.asyncio
async def test_maintenance_metrics_timeout_and_cancelled_wait() -> None:
    coordinator = GlobalSyncCoordinator()

    work_started = asyncio.Event()

    async def slow_work() -> None:
        work_started.set()
        await asyncio.sleep(1)

    with pytest.raises(TimeoutError):
        await coordinator.run_maintenance(slow_work, timeout_=0.01)
    await asyncio.wait_for(work_started.wait(), timeout=1)
    assert coordinator.get_metrics() == {
        "active_profile_syncs": 0,
        "maintenance_active": False,
        "maintenance_waiting": 0,
        "maintenance_duration_seconds": None,
    }

    await coordinator.acquire_profile_slot("default")
    blocked = asyncio.create_task(coordinator.run_maintenance(lambda: asyncio.sleep(0)))
    await asyncio.sleep(0)
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    assert coordinator.get_metrics()["maintenance_waiting"] == 0
    coordinator.release_profile_slot("default")
