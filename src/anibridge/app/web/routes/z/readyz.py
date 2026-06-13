"""Route for readiness check."""

from enum import StrEnum
from typing import Annotated

import msgspec
from litestar.handlers.http_handlers.decorators import get
from litestar.response.base import Response
from litestar.router import Router

from anibridge.app.web.state import get_app_state

__all__ = ["router"]


class ReadyzStatus(StrEnum):
    """Enumerated readiness status values."""

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ReadyzProfilesResponse(msgspec.Struct):
    """Aggregate profile counts exposed by the readiness probe."""

    configured: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Number of configured profiles known to the scheduler.",
            examples=[3],
        ),
    ]
    initialized: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Number of profiles that have been initialized successfully.",
            examples=[3],
        ),
    ]
    failed: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Number of profiles that failed initialization.",
            examples=[0],
        ),
    ]


class ReadyzResponse(msgspec.Struct):
    """Minimal readiness payload for unauthenticated probes."""

    status: Annotated[
        ReadyzStatus,
        msgspec.Meta(
            description="Overall readiness state for the application.",
            examples=["ok"],
        ),
    ]
    ready: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the application is ready to serve sync traffic.",
            examples=[True],
        ),
    ]
    scheduler_running: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the scheduler process is currently running.",
            examples=[True],
        ),
    ]
    profiles: Annotated[
        ReadyzProfilesResponse,
        msgspec.Meta(
            description="Profile initialization summary used to derive readiness.",
            examples=[{"configured": 3, "initialized": 3, "failed": 0}],
        ),
    ]


@get(path="/readyz", include_in_schema=False)
async def readyz() -> Response[ReadyzResponse]:
    """Readiness check endpoint."""
    scheduler = get_app_state().scheduler
    if scheduler is None:
        return Response(
            content=ReadyzResponse(
                status=ReadyzStatus.UNAVAILABLE,
                ready=False,
                scheduler_running=False,
                profiles=ReadyzProfilesResponse(
                    configured=0,
                    initialized=0,
                    failed=0,
                ),
            ),
            status_code=503,
        )

    configured_profiles = len(scheduler.global_config.profiles)
    initialized_profiles = len(scheduler.bridge_clients)
    failed_profiles = len(scheduler.failed_profile_errors)
    scheduler_running = scheduler.is_running
    ready = scheduler_running and failed_profiles == 0

    status = (
        ReadyzStatus.OK
        if ready
        else ReadyzStatus.DEGRADED
        if scheduler_running and initialized_profiles
        else ReadyzStatus.UNAVAILABLE
    )
    return Response(
        content=ReadyzResponse(
            status=status,
            ready=ready,
            scheduler_running=scheduler_running,
            profiles=ReadyzProfilesResponse(
                configured=configured_profiles,
                initialized=initialized_profiles,
                failed=failed_profiles,
            ),
        ),
        status_code=200 if ready else 503,
    )


router = Router(path="", route_handlers=[readyz])
