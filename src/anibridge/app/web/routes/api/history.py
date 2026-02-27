"""History API endpoints."""

from fastapi.param_functions import Query
from fastapi.routing import APIRouter
from pydantic import BaseModel

from anibridge.app.web.services.history_service import (
    HistoryItem,
    HistoryPage,
    get_history_service,
)

router = APIRouter()


class GetHistoryResponse(BaseModel):
    """Paginated history response (flattened)."""

    items: list[HistoryItem]
    page: int
    per_page: int
    total: int
    pages: int
    stats: dict[str, int] = {}


class OkResponse(BaseModel):
    """Response model for successful operations."""

    ok: bool = True


class UndoResponse(BaseModel):
    """Response model for undo operation."""

    item: HistoryItem


class RetryResponse(BaseModel):
    """Response model for retry operation."""

    ok: bool = True


@router.get("/{profile}", response_model=GetHistoryResponse)
async def get_history(
    profile: str,
    page: int = 1,
    per_page: int = 25,
    outcome: str | None = Query(None, description="Filter by outcome"),
    library_namespace: str | None = Query(
        None, description="Filter by library provider namespace"
    ),
    list_namespace: str | None = Query(
        None, description="Filter by list provider namespace"
    ),
) -> GetHistoryResponse:
    """Get paginated timeline for profile.

    Args:
        profile (str): The profile name.
        page (int): The page number.
        per_page (int): The number of items per page.
        outcome (str | None): Filter by outcome.
        library_namespace (str | None): Filter by library provider namespace.
        list_namespace (str | None): Filter by list provider namespace.

    Returns:
        GetHistoryResponse: The paginated history response.

    Raises:
        SchedulerNotInitializedError: If the scheduler is not running.
        ProfileNotFoundError: If the profile is unknown.
    """
    hp: HistoryPage = await get_history_service().get_page(
        profile=profile,
        page=page,
        per_page=per_page,
        outcome=outcome,
        library_namespace=library_namespace,
        list_namespace=list_namespace,
    )
    return GetHistoryResponse(**hp.model_dump())


@router.delete("/{profile}/{item_id}", response_model=OkResponse)
async def delete_history(profile: str, item_id: int) -> OkResponse:
    """Delete a history item.

    Args:
        profile (str): The profile name.
        item_id (int): The ID of the history item to delete.

    Returns:
        OkResponse: The response indicating success.

    Raises:
        HistoryItemNotFoundError: If the specified item does not exist.
    """
    await get_history_service().delete_item(profile, item_id)
    return OkResponse()


@router.post("/{profile}/{item_id}/undo", response_model=UndoResponse)
async def undo_history(profile: str, item_id: int) -> UndoResponse:
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


@router.post("/{profile}/{item_id}/retry", response_model=RetryResponse)
async def retry_history(profile: str, item_id: int) -> RetryResponse:
    """Retry a failed or missing history item.

    Args:
        profile (str): The profile name.
        item_id (int): The ID of the history item to retry.

    Returns:
        RetryResponse: The response indicating success.

    Raises:
        SchedulerNotInitializedError: If the scheduler is not running.
        ProfileNotFoundError: If the profile is unknown.
        HistoryItemNotFoundError: If the specified item does not exist.
        HistoryPermissionError: If the user does not have permission to retry the item.
    """
    await get_history_service().retry_item(profile, item_id)
    return RetryResponse()
