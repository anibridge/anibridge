"""History API endpoints."""

from typing import Annotated

import msgspec
from litestar.handlers.http_handlers.decorators import delete, get, post
from litestar.params import PathParameter, QueryParameter
from litestar.router import Router

from anibridge.app.web.services.history_service import (
    HistoryItem,
    HistoryPage,
    get_history_service,
)

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


class UndoResponse(msgspec.Struct):
    """Response model for undo operation."""

    item: Annotated[
        HistoryItem,
        msgspec.Meta(
            description="History item that was undone and re-recorded.",
            examples=[
                {
                    "id": 42,
                    "profile_name": "default",
                    "outcome": "undone",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                }
            ],
        ),
    ]


class RetryResponse(msgspec.Struct):
    """Response model for retry operation."""

    ok: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the retry request was accepted.",
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
    library_namespace: Annotated[str | None, QueryParameter()] = None,
    list_namespace: Annotated[str | None, QueryParameter()] = None,
) -> GetHistoryResponse:
    """Get paginated timeline for profile."""
    return await get_history_service().get_page(
        profile=profile,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
        outcome=outcome,
        library_namespace=library_namespace,
        list_namespace=list_namespace,
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


@post(path="/{profile:str}/{item_id:int}/undo", status_code=200)
async def undo_history(
    profile: Annotated[str, PathParameter()],
    item_id: Annotated[int, PathParameter()],
) -> UndoResponse:
    """Undo a history item if possible.

    Args:
        profile (str): The profile name.
        item_id (int): The ID of the history item to undo.

    Returns:
        UndoResponse: The response containing the undone item.

    Raises:
        SchedulerNotInitializedError: If the scheduler is not running.
        ProfileNotFoundError: If the profile is unknown.
        HistoryItemNotFoundError: If the specified item does not exist.
    """
    item = await get_history_service().undo_item(profile, item_id)
    return UndoResponse(item=item)


@post(path="/{profile:str}/{item_id:int}/retry", status_code=200)
async def retry_history(
    profile: Annotated[str, PathParameter()],
    item_id: Annotated[int, PathParameter()],
) -> RetryResponse:
    """Retry a failed or missing history item."""
    await get_history_service().retry_item(profile, item_id)
    return RetryResponse()


router = Router(
    path="/history",
    route_handlers=[get_history, delete_history, undo_history, retry_history],
)
