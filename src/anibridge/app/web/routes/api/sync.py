"""API endpoints to trigger sync operations."""

from typing import Annotated

import msgspec
from litestar.handlers.http_handlers.decorators import post
from litestar.params import PathParameter, QueryParameter
from litestar.router import Router

from anibridge.app.core.sync import SyncRequest, SyncTrigger
from anibridge.app.exceptions import SchedulerNotInitializedError
from anibridge.app.utils.async_tasks import schedule_task
from anibridge.app.web.state import get_app_state

__all__ = ["router"]


class OkResponse(msgspec.Struct):
    ok: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the sync request was accepted.",
            examples=[True],
        ),
    ] = True


@post(path="")
async def sync_all(
    trigger: Annotated[SyncTrigger, QueryParameter()] = SyncTrigger.MANUAL,
) -> OkResponse:
    """Trigger a sync for all profiles."""
    scheduler = get_app_state().scheduler
    if not scheduler:
        raise SchedulerNotInitializedError("Scheduler not available")
    schedule_task(
        scheduler.trigger_all_profiles_sync(
            request=SyncRequest(trigger=trigger),
            source="api:sync_all",
        ),
        name="sync_all_profiles",
    )
    return OkResponse(ok=True)


@post(path="/database")
async def sync_database() -> OkResponse:
    """Trigger a sync for the database."""
    scheduler = get_app_state().scheduler
    if not scheduler:
        raise SchedulerNotInitializedError("Scheduler not available")
    schedule_task(
        scheduler.trigger_database_sync(source="api:sync_database"),
        name="sync_database",
    )
    return OkResponse(ok=True)


@post(path="/profile/{profile:str}")
async def sync_profile(
    profile: Annotated[str, PathParameter()],
    trigger: Annotated[SyncTrigger, QueryParameter()] = SyncTrigger.MANUAL,
) -> OkResponse:
    """Trigger a sync for a specific profile."""
    scheduler = get_app_state().scheduler
    if not scheduler:
        raise SchedulerNotInitializedError("Scheduler not available")
    schedule_task(
        scheduler.trigger_profile_sync(
            profile,
            request=SyncRequest(trigger=trigger),
            source="api:sync_profile",
        ),
        name=f"sync_profile:{profile}",
    )
    return OkResponse(ok=True)


@post(path="/profile/{profile:str}/reinitialize")
async def reinitialize_profile(profile: Annotated[str, PathParameter()]) -> OkResponse:
    """Rebuild and restart a single profile."""
    scheduler = get_app_state().scheduler
    if not scheduler:
        raise SchedulerNotInitializedError("Scheduler not available")
    await scheduler.reinitialize_profile(profile)
    return OkResponse(ok=True)


router = Router(
    path="/sync",
    route_handlers=[sync_all, sync_database, sync_profile, reinitialize_profile],
)
