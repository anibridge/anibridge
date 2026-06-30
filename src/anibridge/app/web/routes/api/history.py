"""History API endpoints."""

from typing import Annotated

import msgspec
from litestar.handlers.http_handlers.decorators import delete, get, post
from litestar.params import PathParameter, QueryParameter
from litestar.router import Router

from anibridge.app.web.services.history_service import HistoryPage, get_history_service

__all__ = ["router"]

# TODO: restore the undo endpoint/functionality?

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
    limit: Annotated[int, QueryParameter()] = 25,
    before_id: Annotated[int | None, QueryParameter()] = None,
    after_id: Annotated[int | None, QueryParameter()] = None,
    include_stats: Annotated[bool, QueryParameter()] = True,
    outcome: Annotated[str | None, QueryParameter()] = None,
    source_namespace: Annotated[str | None, QueryParameter()] = None,
    target_namespace: Annotated[str | None, QueryParameter()] = None,
) -> GetHistoryResponse:
    """Get paginated timeline for profile."""
    return await get_history_service().get_page(
        profile=profile,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
        outcome=outcome,
        source_namespace=source_namespace,
        target_namespace=target_namespace,
        include_stats=include_stats,
    )


@delete(path="/{profile:str}/{item_id:int}", status_code=200)
async def delete_history(
    profile: Annotated[str, PathParameter()],
    item_id: Annotated[int, PathParameter()],
) -> OkResponse:
    """Delete a history item."""
    await get_history_service().delete_item(profile, item_id)
    return OkResponse()


@post(path="/{profile:str}/{item_id:int}/retry", status_code=200)
async def retry_history(
    profile: Annotated[str, PathParameter()],
    item_id: Annotated[int, PathParameter()],
) -> RetryResponse:
    """Retry a failed or missing history item."""
    await get_history_service().retry_item(profile, item_id)
    return RetryResponse()


@post(path="/{profile:str}/{item_id:int}/undo", status_code=200)
async def undo_history(
    profile: Annotated[str, PathParameter()],
    item_id: Annotated[int, PathParameter()],
) -> UndoResponse:
    """Undo a synced or deleted history item."""
    await get_history_service().undo_item(profile, item_id)
    return UndoResponse()


router = Router(
    path="/history",
    route_handlers=[get_history, delete_history, retry_history, undo_history],
)
