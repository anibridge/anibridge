"""Tests for status API routes."""

from datetime import UTC, datetime

import pytest

from anibridge.app.web.routes.api import status as status_api_module
from tests.web.support import SchedulerStub


@pytest.mark.asyncio
async def test_status_route_returns_empty_without_scheduler(patch_app_state) -> None:
    patch_app_state(status_api_module, scheduler=None)

    response = await status_api_module.status.fn()

    assert response.profiles == {}
    assert response.scheduler is None


@pytest.mark.asyncio
async def test_status_route_serializes_scheduler_payload(patch_app_state) -> None:
    patch_app_state(
        status_api_module,
        scheduler=SchedulerStub(
            status_payload={
                "primary": {
                    "config": {
                        "source_namespace": "plex",
                        "target_namespace": "anilist",
                        "source_account": "Library User",
                        "target_account": "List User",
                        "poll_interval": 60,
                        "scan_interval": "0 * * * *",
                        "scan_modes": ["poll", "periodic"],
                        "full_scan": True,
                        "destructive_sync": False,
                    },
                    "status": {
                        "running": True,
                        "last_synced": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                        "current_sync": {"state": "running"},
                        "initialization_error": None,
                    },
                }
            },
            runtime_metrics={"coordinator": {"running": True}},
        ),
    )

    response = await status_api_module.status.fn()

    profile = response.profiles["primary"]
    assert profile.config.source_namespace == "plex"
    assert profile.config.target_namespace == "anilist"
    assert profile.config.scan_modes == ["poll", "periodic"]
    assert profile.status.current_sync == {"state": "running"}
    assert response.scheduler == {"coordinator": {"running": True}}


def test_construct_profile_status_handles_missing_fields() -> None:
    profile = status_api_module.construct_profile_status(
        {"config": {"source_namespace": "plex", "target_namespace": "anilist"}}
    )

    assert profile.config.source_namespace == "plex"
    assert profile.status.running is False
    assert profile.status.last_synced is None
