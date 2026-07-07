"""History API endpoints."""

from typing import Annotated

import msgspec
from litestar.exceptions.http_exceptions import HTTPException
from litestar.handlers.http_handlers.decorators import delete, get, post
from litestar.params import PathParameter, QueryParameter
from litestar.router import Router

from anibridge.app.web.services.history_service import HistoryPage, get_history_service

__all__ = ["router"]

GetHistoryResponse = HistoryPage


class OkResponse(msgspec.Struct):
    """Response model for successful operations."""

    ok: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the operation completed successfully.",
            examples=[True],
        ),
    ] = True


class RetryResponse(msgspec.Struct):
    """Response model for retry operation."""

    ok: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the retry request was accepted.",
            examples=[True],
        ),
    ] = True


class UndoResponse(msgspec.Struct):
    """Response model for undo operation."""

    ok: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the undo request was accepted.",
            examples=[True],
        ),
    ] = True


@get(path="/{profile:str}")
async def get_history(
    profile: Annotated[str, PathParameter()],
    limit: Annotated[int, QueryParameter(ge=1, le=250)] = 25,
    before_id: Annotated[int | None, QueryParameter()] = None,
    after_id: Annotated[int | None, QueryParameter()] = None,
    include_stats: Annotated[bool, QueryParameter()] = True,
    outcome: Annotated[str | None, QueryParameter()] = None,
    source_namespace: Annotated[str | None, QueryParameter()] = None,
    target_namespace: Annotated[str | None, QueryParameter()] = None,
    resource_kind: Annotated[str | None, QueryParameter()] = None,
) -> GetHistoryResponse:
    """Get paginated timeline for profile."""
    if before_id is not None and after_id is not None:
        raise HTTPException(
            status_code=400,
            detail="before_id and after_id are mutually exclusive",
        )

    return await get_history_service().get_page(
        profile=profile,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
        outcome=outcome,
        source_namespace=source_namespace,
        target_namespace=target_namespace,
        resource_kind=resource_kind,
        include_stats=include_stats,
    )


@delete(path="/{profile:str}/groups/{group_id:int}", status_code=200)
async def delete_history_group(
    profile: Annotated[str, PathParameter()],
    group_id: Annotated[int, PathParameter()],
) -> OkResponse:
    """Delete a history group."""
    await get_history_service().delete_group(profile, group_id)
    return OkResponse()


@delete(path="/{profile:str}/operations/{operation_id:int}", status_code=200)
async def delete_history_operation(
    profile: Annotated[str, PathParameter()],
    operation_id: Annotated[int, PathParameter()],
) -> OkResponse:
    """Delete a history operation."""
    await get_history_service().delete_operation(profile, operation_id)
    return OkResponse()


@post(path="/{profile:str}/groups/{group_id:int}/retry", status_code=200)
async def retry_history_group(
    profile: Annotated[str, PathParameter()],
    group_id: Annotated[int, PathParameter()],
) -> RetryResponse:
    """Retry a failed or missing history group."""
    await get_history_service().retry_group(profile, group_id)
    return RetryResponse()


@post(path="/{profile:str}/operations/{operation_id:int}/undo", status_code=200)
async def undo_history_operation(
    profile: Annotated[str, PathParameter()],
    operation_id: Annotated[int, PathParameter()],
) -> UndoResponse:
    """Undo a synced or deleted record operation."""
    await get_history_service().undo_operation(profile, operation_id)
    return UndoResponse()


router = Router(
    path="/history",
    route_handlers=[
        get_history,
        delete_history_group,
        delete_history_operation,
        retry_history_group,
        undo_history_operation,
    ],
)
