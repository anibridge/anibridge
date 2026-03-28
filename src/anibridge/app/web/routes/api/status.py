"""API status endpoints."""

from fastapi.routing import APIRouter
from pydantic import BaseModel

from anibridge.app.web.state import get_app_state

__all__ = [
    "ProfileConfigModel",
    "ProfileRuntimeStatusModel",
    "ProfileStatusModel",
    "construct_profile_status",
    "router",
]


class ProfileConfigModel(BaseModel):
    """Serialized profile configuration exposed to the web UI."""

    library_namespace: str
    list_namespace: str
    library_user: str | None = None
    list_user: str | None = None
    poll_interval: int | str | None = None
    scan_interval: int | str | None = None
    scan_modes: list[str] = []
    full_scan: bool | None = None
    destructive_sync: bool | None = None


class ProfileRuntimeStatusModel(BaseModel):
    """Runtime status of a profile exposed to the web UI."""

    running: bool
    last_synced: str | None = None
    current_sync: dict | None = None


class ProfileStatusModel(BaseModel):
    """Combined profile configuration and runtime status exposed to the web UI."""

    config: ProfileConfigModel
    status: ProfileRuntimeStatusModel


class StatusResponse(BaseModel):
    profiles: dict[str, ProfileStatusModel]
    scheduler: dict | None = None


router = APIRouter()


@router.get("", response_model=StatusResponse)
async def status() -> StatusResponse:
    """Get the status of the application.

    Returns:
        dict[str, Any]: The status of the application.
    """
    scheduler = get_app_state().scheduler
    if not scheduler:
        return StatusResponse.model_construct(profiles={}, scheduler=None)
    raw = await scheduler.get_status()
    runtime_metrics = await scheduler.get_runtime_metrics()
    converted = {name: construct_profile_status(data) for name, data in raw.items()}
    return StatusResponse.model_construct(profiles=converted, scheduler=runtime_metrics)


def construct_profile_status(data: dict) -> ProfileStatusModel:
    """Build trusted scheduler profile payloads without re-validating them."""
    cfg = data.get("config", {})
    st = data.get("status", {})
    return ProfileStatusModel.model_construct(
        config=ProfileConfigModel.model_construct(
            library_namespace=cfg.get("library_namespace"),
            list_namespace=cfg.get("list_namespace"),
            library_user=cfg.get("library_user"),
            list_user=cfg.get("list_user"),
            poll_interval=cfg.get("poll_interval"),
            scan_interval=cfg.get("scan_interval"),
            scan_modes=list(cfg.get("scan_modes") or []),
            full_scan=cfg.get("full_scan"),
            destructive_sync=cfg.get("destructive_sync"),
        ),
        status=ProfileRuntimeStatusModel.model_construct(
            running=bool(st.get("running")),
            last_synced=st.get("last_synced"),
            current_sync=st.get("current_sync"),
        ),
    )
