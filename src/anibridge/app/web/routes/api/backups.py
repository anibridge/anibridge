"""Backup API endpoints."""

from typing import Annotated, Any

import msgspec
from litestar.handlers.http_handlers.decorators import get, post
from litestar.params import Body, PathParameter
from litestar.router import Router

from anibridge.app.web.services.backup_service import BackupMeta, get_backup_service

__all__ = ["router"]


class ListBackupsResponse(msgspec.Struct):
    """Response model for listing backups."""

    backups: Annotated[
        list[BackupMeta],
        msgspec.Meta(
            description="Available backup files for the requested profile.",
            examples=[[{"filename": "anibridge_default_anilist_20260508120000.json"}]],
        ),
    ]


class RestoreRequest(msgspec.Struct):
    """Request body for triggering a restore."""

    filename: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Backup file name to restore for the selected profile.",
            examples=["anibridge_default_anilist_20260508120000.json"],
        ),
    ]


@get(path="/{profile:str}", sync_to_thread=True)
def list_backups(profile: Annotated[str, PathParameter()]) -> ListBackupsResponse:
    """List backups for a profile."""
    backups = get_backup_service().list_backups(profile)
    return ListBackupsResponse(backups=backups)


@post(path="/{profile:str}/restore", status_code=200)
async def restore_backup(
    profile: Annotated[str, PathParameter()], data: Annotated[RestoreRequest, Body()]
) -> None:
    """Restore a backup file (no dry-run mode)."""
    await get_backup_service().restore_backup(profile=profile, filename=data.filename)


@get(path="/{profile:str}/raw/{filename:str}", sync_to_thread=True)
def get_backup_raw(
    profile: Annotated[str, PathParameter()], filename: Annotated[str, PathParameter()]
) -> Any:
    """Return raw JSON content of a backup.

    The response is unvalidated JSON so the UI can present a preview.
    """
    return get_backup_service().read_backup_raw(profile, filename)


router = Router(
    path="/backups",
    route_handlers=[list_backups, restore_backup, get_backup_raw],
)
