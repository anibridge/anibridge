"""API endpoints to trigger sync operations."""

from fastapi.param_functions import Body, Path, Query
from fastapi.routing import APIRouter
from pydantic import BaseModel

from anibridge.app.exceptions import SchedulerNotInitializedError
from anibridge.app.utils.async_tasks import schedule_task
from anibridge.app.web.state import get_app_state

__all__ = ["router"]


class OkResponse(BaseModel):
    ok: bool = True


router = APIRouter()


@router.post("", response_model=OkResponse)
async def sync_all(poll: bool = Query(False)) -> OkResponse:
    """Trigger a sync for all profiles.

    Args:
        poll (bool): Whether to poll for updates.

    Returns:
        OkResponse: The response containing the sync status.

    Raises:
        SchedulerNotInitializedError: If the scheduler is not running.
    """
    scheduler = get_app_state().scheduler
    if not scheduler:
        raise SchedulerNotInitializedError("Scheduler not available")
    schedule_task(
        scheduler.trigger_all_profiles_sync(poll=poll, source="api:sync_all"),
        name="sync_all_profiles",
    )
    return OkResponse(ok=True)


@router.post("/database", response_model=OkResponse)
async def sync_database() -> OkResponse:
    """Trigger a sync for the database.

    Returns:
        OkResponse: The response containing the sync status.

    Raises:
        SchedulerNotInitializedError: If the scheduler is not running.
    """
    scheduler = get_app_state().scheduler
    if not scheduler:
        raise SchedulerNotInitializedError("Scheduler not available")
    schedule_task(
        scheduler.trigger_database_sync(source="api:sync_database"),
        name="sync_database",
    )
    return OkResponse(ok=True)


@router.post("/profile/{profile}", response_model=OkResponse)
async def sync_profile(
    profile: str = Path(...),
    poll: bool = Query(False),
    library_keys: list[str] | None = Body(default=None, embed=True),
) -> OkResponse:
    """Trigger a sync for a specific profile.

    Args:
        profile (str): The profile to sync.
        poll (bool): Whether to poll for updates.
        library_keys (list[str] | None): Specific rating keys to sync (if any).

    Returns:
        OkResponse: The response containing the sync status.

    Raises:
        SchedulerNotInitializedError: If the scheduler is not running.
        ProfileNotFoundError: If the profile does not exist.
    """
    scheduler = get_app_state().scheduler
    if not scheduler:
        raise SchedulerNotInitializedError("Scheduler not available")
    schedule_task(
        scheduler.trigger_profile_sync(
            profile,
            poll=poll,
            library_keys=library_keys,
            source="api:sync_profile",
        ),
        name=f"sync_profile:{profile}",
    )
    return OkResponse(ok=True)
