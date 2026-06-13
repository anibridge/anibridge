"""Tests for sync API endpoints."""

from collections.abc import Coroutine

import pytest

from anibridge.app.core.sync.request import SyncTrigger
from anibridge.app.exceptions import SchedulerNotInitializedError
from anibridge.app.web.routes.api import sync as sync_api_module
from tests.web.support import SchedulerStub


@pytest.fixture
def scheduler() -> SchedulerStub:
    return SchedulerStub()


@pytest.fixture
def scheduled_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, Coroutine[object, object, None]]]:
    tasks: list[tuple[str, Coroutine[object, object, None]]] = []

    def _schedule_task(coro: Coroutine[object, object, None], *, name: str) -> None:
        tasks.append((name, coro))
        coro.close()

    monkeypatch.setattr(sync_api_module, "schedule_task", _schedule_task)
    return tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_task_name"),
    [
        pytest.param("all", "sync_all_profiles", id="all-profiles"),
        pytest.param("database", "sync_database", id="database"),
        pytest.param("profile", "sync_profile:broken", id="single-profile"),
    ],
)
async def test_sync_routes_schedule_background_tasks(
    patch_app_state,
    scheduler: SchedulerStub,
    scheduled_tasks,
    operation: str,
    expected_task_name: str,
) -> None:
    patch_app_state(sync_api_module, scheduler=scheduler)

    if operation == "all":
        response = await sync_api_module.sync_all.fn(trigger=SyncTrigger.POLL)
    elif operation == "database":
        response = await sync_api_module.sync_database.fn()
    else:
        response = await sync_api_module.sync_profile.fn(
            "broken", trigger=SyncTrigger.POLL
        )

    assert response.ok is True
    assert [name for name, _ in scheduled_tasks] == [expected_task_name]


@pytest.mark.asyncio
async def test_reinitialize_profile_calls_scheduler(
    patch_app_state,
    scheduler: SchedulerStub,
) -> None:
    """Reinitialize endpoint should target the requested profile."""
    patch_app_state(sync_api_module, scheduler=scheduler)

    response = await sync_api_module.reinitialize_profile.fn("broken")

    assert response.ok is True
    assert scheduler.reinitialized_profiles == ["broken"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("all", id="all-profiles"),
        pytest.param("database", id="database"),
        pytest.param("profile", id="single-profile"),
        pytest.param("reinitialize", id="reinitialize"),
    ],
)
async def test_sync_routes_require_scheduler(
    patch_app_state,
    operation: str,
) -> None:
    patch_app_state(sync_api_module, scheduler=None)

    with pytest.raises(SchedulerNotInitializedError):
        if operation == "all":
            await sync_api_module.sync_all.fn()
        elif operation == "database":
            await sync_api_module.sync_database.fn()
        elif operation == "profile":
            await sync_api_module.sync_profile.fn("broken")
        else:
            await sync_api_module.reinitialize_profile.fn("broken")
