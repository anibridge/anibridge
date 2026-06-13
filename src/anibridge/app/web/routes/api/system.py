"""System related API endpoints (settings dump, about/runtime info)."""

import os
import platform
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any

import msgspec
import psutil
from litestar.handlers.http_handlers.decorators import get, post
from litestar.router import Router

from anibridge.app import __git_hash__, __version__
from anibridge.app.exceptions import AnibridgeError, SchedulerUnavailableError
from anibridge.app.utils.human import human_duration
from anibridge.app.web.routes.api.config import require_config_api_access
from anibridge.app.web.routes.api.status import (
    ProfileStatusModel,
    construct_profile_status,
)
from anibridge.app.web.state import get_app_state

__all__ = ["router"]


class SettingsProfileModel(msgspec.Struct):
    name: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Profile name from the current configuration.",
            examples=["default"],
        ),
    ]
    settings: Annotated[
        dict[str, Any],
        msgspec.Meta(
            description="Serialized profile settings payload.",
            examples=[
                {"source": {"namespace": "plex"}, "target": {"namespace": "anilist"}}
            ],
        ),
    ]


class SettingsResponse(msgspec.Struct):
    global_config: Annotated[
        dict[str, Any],
        msgspec.Meta(
            description=(
                "Serialized global AniBridge configuration without per-profile entries."
            ),
            examples=[{"web_enabled": True, "log_level": "INFO"}],
        ),
    ]
    profiles: Annotated[
        list[SettingsProfileModel],
        msgspec.Meta(
            description="Per-profile configuration payloads.",
            examples=[
                [{"name": "default", "settings": {"source": {"namespace": "plex"}}}]
            ],
        ),
    ]


class AboutInfoModel(msgspec.Struct):
    version: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Running AniBridge version.",
            examples=["2.1.4"],
        ),
    ]
    git_hash: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Git commit hash for the running build.",
            examples=["abc123def456"],
        ),
    ]
    python: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Python runtime version.",
            examples=["3.14.3"],
        ),
    ]
    platform: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Operating system and platform string.",
            examples=["Linux-6.8.0-x86_64-with-glibc2.39"],
        ),
    ]
    utc_now: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Current UTC timestamp when the payload was generated.",
            examples=["2026-01-01T00:00:00+00:00"],
        ),
    ]
    started_at: (
        Annotated[
            str,
            msgspec.Meta(
                description="UTC timestamp when the process started.",
                examples=["2026-01-01T00:00:00+00:00"],
            ),
        ]
        | None
    ) = None
    uptime_seconds: (
        Annotated[
            int,
            msgspec.Meta(
                ge=0,
                description="Process uptime in seconds.",
                examples=[3600],
            ),
        ]
        | None
    ) = None
    uptime: (
        Annotated[
            str,
            msgspec.Meta(
                description="Human-readable process uptime.",
                examples=["1h"],
            ),
        ]
        | None
    ) = None
    sqlite: (
        Annotated[
            str,
            msgspec.Meta(
                description="SQLite library version linked into the process.",
                examples=["3.46.0"],
            ),
        ]
        | None
    ) = None


class ProcessInfoModel(msgspec.Struct):
    pid: Annotated[
        int,
        msgspec.Meta(
            ge=1,
            description="Current AniBridge process identifier.",
            examples=[1234],
        ),
    ]
    cpu_count: (
        Annotated[
            int,
            msgspec.Meta(
                ge=1,
                description="Visible logical CPU count for the running process.",
                examples=[8],
            ),
        ]
        | None
    ) = None
    memory_mb: (
        Annotated[
            float,
            msgspec.Meta(
                ge=0,
                description="Memory usage of the process in megabytes.",
                examples=[128.5],
            ),
        ]
        | None
    ) = None


class SchedulerSummaryModel(msgspec.Struct):
    running: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the scheduler process is currently active.",
            examples=[True],
        ),
    ]
    configured_profiles: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Number of profiles configured in the current settings file.",
            examples=[3],
        ),
    ]
    total_profiles: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Number of profile status entries currently available.",
            examples=[3],
        ),
    ]
    running_profiles: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Number of profiles whose runtime state is active.",
            examples=[2],
        ),
    ]
    syncing_profiles: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Number of profiles actively syncing at the moment.",
            examples=[1],
        ),
    ]
    sync_mode_counts: Annotated[
        dict[str, int],
        msgspec.Meta(
            description="Count of profiles participating in each configured scan mode.",
            examples=[{"poll": 2, "webhook": 1}],
        ),
    ]
    profiles: Annotated[
        dict[str, ProfileStatusModel],
        msgspec.Meta(
            description="Per-profile scheduler summary keyed by profile name.",
            examples=[
                {
                    "default": {
                        "config": {
                            "source_namespace": "plex",
                            "target_namespace": "anilist",
                        },
                        "status": {"running": True},
                    }
                }
            ],
        ),
    ]
    most_recent_sync: (
        Annotated[
            str,
            msgspec.Meta(
                description=(
                    "ISO-8601 timestamp of the most recent completed "
                    "sync across all profiles."
                ),
                examples=["2026-01-01T00:00:00+00:00"],
            ),
        ]
        | None
    ) = None
    most_recent_sync_profile: (
        Annotated[
            str,
            msgspec.Meta(
                description=(
                    "Profile name associated with the most recent completed sync."
                ),
                examples=["default"],
            ),
        ]
        | None
    ) = None
    next_database_sync_at: (
        Annotated[
            str,
            msgspec.Meta(
                description="ISO-8601 timestamp for the next scheduled database sync.",
                examples=["2026-01-01T13:00:00+00:00"],
            ),
        ]
        | None
    ) = None
    coordinator: (
        Annotated[
            dict[str, Any],
            msgspec.Meta(
                description=(
                    "Low-level coordinator state returned by the "
                    "scheduler runtime metrics."
                ),
                examples=[{"active_profiles": ["default"], "queued_profiles": []}],
            ),
        ]
        | None
    ) = None


class AboutResponse(msgspec.Struct):
    info: Annotated[
        AboutInfoModel,
        msgspec.Meta(
            description="General runtime metadata about the AniBridge process.",
            examples=[{"version": "2.1.4", "git_hash": "abc123def456"}],
        ),
    ]
    process: Annotated[
        ProcessInfoModel,
        msgspec.Meta(
            description="Current process resource information.",
            examples=[{"pid": 1234, "cpu_count": 8, "memory_mb": 128.5}],
        ),
    ]
    scheduler: Annotated[
        SchedulerSummaryModel,
        msgspec.Meta(
            description="Aggregated scheduler status and profile summary.",
            examples=[
                {
                    "running": True,
                    "configured_profiles": 3,
                    "total_profiles": 3,
                    "running_profiles": 2,
                    "syncing_profiles": 1,
                    "sync_mode_counts": {"poll": 2},
                    "profiles": {},
                }
            ],
        ),
    ]
    status: Annotated[
        dict[str, ProfileStatusModel],
        msgspec.Meta(
            description="Raw per-profile status payload keyed by profile name.",
            examples=[
                {
                    "default": {
                        "config": {
                            "source_namespace": "plex",
                            "target_namespace": "anilist",
                        },
                        "status": {"running": True},
                    }
                }
            ],
        ),
    ]


class MetaResponse(msgspec.Struct):
    version: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Running AniBridge version.",
            examples=["2.1.4"],
        ),
    ]
    git_hash: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Git commit hash for the running build.",
            examples=["abc123def456"],
        ),
    ]


class RestartResponse(msgspec.Struct):
    ok: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the restart request was accepted.",
            examples=[True],
        ),
    ]
    message: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Human-readable result message for the restart request.",
            examples=["Restart requested. AniBridge will restart shortly."],
        ),
    ]


@get(path="/settings", sync_to_thread=True)
def api_settings() -> SettingsResponse:
    """Return the current application configuration as JSON."""
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


@get(path="/about")
async def api_about() -> AboutResponse:
    """Get runtime metadata."""
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
        converted[name] = construct_profile_status(data)

        if converted[name].status.running:
            running_profiles += 1

        current_sync = converted[name].status.current_sync
        if current_sync is not None and current_sync.get("state") == "running":
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
        profiles=converted,
        most_recent_sync=most_recent_sync_iso,
        most_recent_sync_profile=most_recent_sync_profile,
        next_database_sync_at=next_db_sync_iso,
        coordinator=scheduler_runtime_metrics.get("coordinator"),
    )

    pid = os.getpid()
    cpu_count = psutil.cpu_count(logical=True)
    memory_mb = psutil.Process(pid).memory_info().rss / (1024 * 1024)
    process_info = ProcessInfoModel(pid=pid, cpu_count=cpu_count, memory_mb=memory_mb)

    return AboutResponse(
        info=info,
        process=process_info,
        scheduler=scheduler_summary,
        status=converted,
    )


@get(path="/meta", sync_to_thread=True)
def meta() -> MetaResponse:
    """Application metadata (version, git hash)."""
    return MetaResponse(version=__version__, git_hash=__git_hash__)


@post(path="/restart", status_code=202, sync_to_thread=True)
def api_restart() -> RestartResponse:
    """Request a graceful scheduler shutdown and process restart."""
    require_config_api_access()
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


router = Router(
    path="/system",
    route_handlers=[api_settings, api_about, meta, api_restart],
)
