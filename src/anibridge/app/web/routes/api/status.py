"""API status endpoints."""

from typing import Annotated, Any

import msgspec
from litestar.handlers.http_handlers.decorators import get
from litestar.router import Router

from anibridge.app.web.state import get_app_state

__all__ = [
    "ProfileConfigModel",
    "ProfileRuntimeStatusModel",
    "ProfileStatusModel",
    "construct_profile_status",
    "router",
    "status",
]


class ProfileConfigModel(msgspec.Struct):
    """Serialized profile configuration exposed to the web UI."""

    source_namespace: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Configured source provider namespace.",
            examples=["plex"],
        ),
    ]
    target_namespace: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Configured target provider namespace.",
            examples=["anilist"],
        ),
    ]
    source_account: (
        Annotated[
            str,
            msgspec.Meta(
                description=(
                    "Source-side account or profile label used by the sync profile."
                ),
                examples=["DemoUser"],
            ),
        ]
        | None
    ) = None
    target_account: (
        Annotated[
            str,
            msgspec.Meta(
                description="Target-provider account label used by the sync profile.",
                examples=["AniListUser"],
            ),
        ]
        | None
    ) = None
    poll_interval: (
        Annotated[
            int | str,
            msgspec.Meta(
                description=(
                    "Configured polling interval for the profile "
                    "when polling is enabled."
                ),
                examples=[300],
            ),
        ]
        | None
    ) = None
    scan_interval: (
        Annotated[
            int | str,
            msgspec.Meta(
                description="Configured scheduled scan interval for the profile.",
                examples=[3600],
            ),
        ]
        | None
    ) = None
    scan_modes: Annotated[
        list[str],
        msgspec.Meta(
            description="Enabled scan modes for the profile.",
            examples=[["poll", "webhook"]],
        ),
    ] = msgspec.field(default_factory=list)
    full_scan: (
        Annotated[
            bool,
            msgspec.Meta(
                description="Whether full scans are enabled for the profile.",
                examples=[False],
            ),
        ]
        | None
    ) = None
    destructive_sync: (
        Annotated[
            bool,
            msgspec.Meta(
                description=(
                    "Whether destructive sync behavior is enabled for the profile."
                ),
                examples=[False],
            ),
        ]
        | None
    ) = None


class ProfileRuntimeStatusModel(msgspec.Struct):
    """Runtime status of a profile exposed to the web UI."""

    running: Annotated[
        bool,
        msgspec.Meta(
            description="Whether the profile runtime is currently active.",
            examples=[True],
        ),
    ]
    last_synced: (
        Annotated[
            str,
            msgspec.Meta(
                description="ISO-8601 timestamp of the most recent completed sync.",
                examples=["2026-01-01T00:00:00+00:00"],
            ),
        ]
        | None
    ) = None
    current_sync: (
        Annotated[
            dict[str, Any],
            msgspec.Meta(
                description=(
                    "Live sync progress payload when the profile is actively syncing."
                ),
                examples=[
                    {
                        "state": "running",
                        "stage": "processing",
                        "processed_items": 5,
                        "scanned_items": 8,
                        "total_items": 12,
                    }
                ],
            ),
        ]
        | None
    ) = None
    initialization_error: (
        Annotated[
            str,
            msgspec.Meta(
                description=(
                    "Initialization error message when the profile failed to start."
                ),
                examples=["Invalid library token"],
            ),
        ]
        | None
    ) = None


class ProfileStatusModel(msgspec.Struct):
    """Combined profile configuration and runtime status exposed to the web UI."""

    config: Annotated[
        ProfileConfigModel,
        msgspec.Meta(
            description="Static configuration summary for the profile.",
            examples=[{"source_namespace": "plex", "target_namespace": "anilist"}],
        ),
    ]
    status: Annotated[
        ProfileRuntimeStatusModel,
        msgspec.Meta(
            description="Live runtime state for the profile.",
            examples=[{"running": True, "last_synced": "2026-01-01T00:00:00+00:00"}],
        ),
    ]


class StatusResponse(msgspec.Struct):
    profiles: Annotated[
        dict[str, ProfileStatusModel],
        msgspec.Meta(
            description="Per-profile status payload keyed by profile name.",
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
    scheduler: (
        Annotated[
            dict[str, Any],
            msgspec.Meta(
                description=(
                    "Scheduler runtime metrics when the scheduler is available."
                ),
                examples=[{"running_profiles": 1, "queued_jobs": 0}],
            ),
        ]
        | None
    ) = None


@get(path="")
async def status() -> StatusResponse:
    """Get the status of the application."""
    scheduler = get_app_state().scheduler
    if not scheduler:
        return StatusResponse(profiles={}, scheduler=None)
    raw = await scheduler.get_status()
    runtime_metrics = await scheduler.get_runtime_metrics()
    converted = {name: construct_profile_status(data) for name, data in raw.items()}
    return StatusResponse(profiles=converted, scheduler=runtime_metrics)


def construct_profile_status(data: dict[str, Any]) -> ProfileStatusModel:
    """Build trusted scheduler profile payloads without re-validating them."""
    cfg = data.get("config", {})
    st = data.get("status", {})
    return ProfileStatusModel(
        config=ProfileConfigModel(
            source_namespace=cfg.get("source_namespace"),
            target_namespace=cfg.get("target_namespace"),
            source_account=cfg.get("source_account"),
            target_account=cfg.get("target_account"),
            poll_interval=cfg.get("poll_interval"),
            scan_interval=cfg.get("scan_interval"),
            scan_modes=list(cfg.get("scan_modes") or []),
            full_scan=cfg.get("full_scan"),
            destructive_sync=cfg.get("destructive_sync"),
        ),
        status=ProfileRuntimeStatusModel(
            running=bool(st.get("running")),
            last_synced=st.get("last_synced"),
            current_sync=st.get("current_sync"),
            initialization_error=st.get("initialization_error"),
        ),
    )


router = Router(path="/status", route_handlers=[status])
