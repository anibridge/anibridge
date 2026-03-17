"""System related API endpoints (settings dump, about/runtime info)."""

import os
import platform
import sqlite3
import sys
from datetime import UTC, datetime
from typing import Any

try:
    import resource
except ImportError:  # Windows does not have resource module
    resource = None  # ty:ignore[invalid-assignment]

from fastapi import Depends
from fastapi.routing import APIRouter
from pydantic import BaseModel

from anibridge.app import __git_hash__, __version__
from anibridge.app.exceptions import AnibridgeError, SchedulerUnavailableError
from anibridge.app.utils.human import human_duration
from anibridge.app.web.routes.api.config import require_config_api_access
from anibridge.app.web.routes.api.status import (
    ProfileConfigModel,
    ProfileRuntimeStatusModel,
    ProfileStatusModel,
)
from anibridge.app.web.state import get_app_state

__all__ = ["router"]


class SettingsProfileModel(BaseModel):
    name: str
    settings: dict[str, Any]


class SettingsResponse(BaseModel):
    global_config: dict[str, Any]
    profiles: list[SettingsProfileModel]


class AboutInfoModel(BaseModel):
    version: str
    git_hash: str
    python: str
    platform: str
    utc_now: str
    started_at: str | None = None
    uptime_seconds: int | None = None
    uptime: str | None = None
    sqlite: str | None = None


class ProcessInfoModel(BaseModel):
    pid: int
    cpu_count: int | None = None
    memory_mb: float | None = None


class SchedulerSummaryModel(BaseModel):
    running: bool
    configured_profiles: int
    total_profiles: int
    running_profiles: int
    syncing_profiles: int
    sync_mode_counts: dict[str, int]
    most_recent_sync: str | None = None
    most_recent_sync_profile: str | None = None
    next_database_sync_at: str | None = None
    coordinator: dict | None = None
    profiles: dict[str, ProfileStatusModel]


class AboutResponse(BaseModel):
    info: AboutInfoModel
    process: ProcessInfoModel
    scheduler: SchedulerSummaryModel
    status: dict[str, ProfileStatusModel]


class MetaResponse(BaseModel):
    version: str
    git_hash: str


class RestartResponse(BaseModel):
    ok: bool
    message: str


router = APIRouter()


@router.get(
    "/settings",
    summary="Return serialized configuration",
    response_model=SettingsResponse,
)
def api_settings() -> SettingsResponse:
    """Return the current application configuration as JSON.

    Returns:
        dict[str, Any]: The serialized configuration.
    """
    scheduler = get_app_state().scheduler
    if not scheduler:
        return SettingsResponse(global_config={}, profiles=[])

    global_config = scheduler.global_config.model_dump(
        mode="json", exclude={"profiles"}
    )
    profiles = [
        SettingsProfileModel(name=name, settings=pdata.model_dump(mode="json"))
        for name, pdata in scheduler.global_config.profiles.items()
    ]

    return SettingsResponse(global_config=global_config, profiles=profiles)


@router.get(
    "/about",
    summary="Return runtime & scheduler diagnostics",
    response_model=AboutResponse,
)
async def api_about() -> AboutResponse:
    """Get runtime metadata.

    Returns:
        dict[str, Any]: The runtime metadata.

    Raises:
        SchedulerUnavailableError: If scheduler status cannot be retrieved.
        AnibridgeError: Any domain error raised by underlying components.
    """
    scheduler = get_app_state().scheduler
    status: dict[str, Any] = {}
    scheduler_runtime_metrics: dict[str, Any] = {}
    scheduler_running = False
    next_db_sync_iso: str | None = None

    if scheduler:
        try:
            status = await scheduler.get_status()
            scheduler_runtime_metrics = await scheduler.get_runtime_metrics()
            scheduler_running = scheduler.is_running
            next_db_sync = scheduler.get_next_database_sync_at()
            if next_db_sync is not None:
                next_db_sync_iso = next_db_sync.isoformat()
        except AnibridgeError:
            raise
        except Exception as e:
            raise SchedulerUnavailableError(
                f"Unable to fetch scheduler status: {e}"
            ) from e

    started_at = get_app_state().started_at
    now = datetime.now(UTC)
    uptime_seconds: int | None = None
    human_uptime: str | None = None

    if started_at:
        delta = now - started_at
        uptime_seconds = int(delta.total_seconds())
        human_uptime = human_duration(uptime_seconds)

    info = AboutInfoModel(
        version=__version__,
        git_hash=__git_hash__,
        python=platform.python_version(),
        platform=platform.platform(),
        utc_now=now.isoformat(),
        started_at=started_at.isoformat() if started_at else None,
        uptime_seconds=uptime_seconds,
        uptime=human_uptime,
        sqlite=sqlite3.sqlite_version,
    )

    converted: dict[str, ProfileStatusModel] = {}
    sync_mode_counts: dict[str, int] = {}
    running_profiles = 0
    syncing_profiles = 0
    most_recent_sync_dt: datetime | None = None
    most_recent_sync_profile: str | None = None

    for name, data in status.items():
        cfg = data.get("config", {})
        st = data.get("status", {})
        converted[name] = ProfileStatusModel(
            config=ProfileConfigModel(**cfg), status=ProfileRuntimeStatusModel(**st)
        )

        if converted[name].status.running:
            running_profiles += 1

        current_sync = converted[name].status.current_sync
        if current_sync is not None:
            syncing_profiles += 1

        for mode in converted[name].config.scan_modes:
            sync_mode_counts[mode] = sync_mode_counts.get(mode, 0) + 1

        last_synced = converted[name].status.last_synced
        if last_synced:
            try:
                parsed = datetime.fromisoformat(last_synced)
            except ValueError:
                parsed = None
            if parsed is not None and (
                most_recent_sync_dt is None or parsed > most_recent_sync_dt
            ):
                most_recent_sync_dt = parsed
                most_recent_sync_profile = name

    most_recent_sync_iso = (
        most_recent_sync_dt.isoformat() if most_recent_sync_dt is not None else None
    )

    configured_profiles = (
        len(scheduler.global_config.profiles)
        if scheduler and scheduler.global_config
        else 0
    )

    scheduler_summary = SchedulerSummaryModel(
        running=scheduler_running,
        configured_profiles=configured_profiles,
        total_profiles=len(converted),
        running_profiles=running_profiles,
        syncing_profiles=syncing_profiles,
        sync_mode_counts=sync_mode_counts,
        most_recent_sync=most_recent_sync_iso,
        most_recent_sync_profile=most_recent_sync_profile,
        next_database_sync_at=next_db_sync_iso,
        coordinator=scheduler_runtime_metrics.get("coordinator"),
        profiles=converted,
    )

    pid = os.getpid()
    cpu_count = os.cpu_count()
    memory_mb: float | None = None
    if resource is not None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss = getattr(usage, "ru_maxrss", None)
        if rss is not None:
            if sys.platform == "darwin":
                memory_mb = round(rss / (1024 * 1024), 2)
            else:
                memory_mb = round(rss / 1024, 2)

    process_info = ProcessInfoModel(pid=pid, cpu_count=cpu_count, memory_mb=memory_mb)

    return AboutResponse(
        info=info,
        process=process_info,
        scheduler=scheduler_summary,
        status=converted,
    )


@router.get("/meta", tags=["meta"], response_model=MetaResponse)
def meta() -> MetaResponse:
    """Application metadata (version, git hash).

    Returns:
        dict[str, str]: The application metadata.
    """
    return MetaResponse(version=__version__, git_hash=__git_hash__)


@router.post(
    "/restart",
    summary="Request graceful server restart",
    dependencies=[Depends(require_config_api_access)],
    response_model=RestartResponse,
    status_code=202,
)
def api_restart() -> RestartResponse:
    """Request a graceful scheduler shutdown and process restart.

    Returns:
        RestartResponse: Accepted restart request status.

    Raises:
        SchedulerUnavailableError: If scheduler is unavailable.
    """
    app_state = get_app_state()
    scheduler = app_state.scheduler
    if not scheduler:
        raise SchedulerUnavailableError(
            "Scheduler not available; restart unsupported in this mode"
        )

    app_state.request_restart()
    scheduler.request_shutdown()

    return RestartResponse(
        ok=True,
        message="Restart requested. AniBridge will restart shortly.",
    )
