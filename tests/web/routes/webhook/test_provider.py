"""Tests for provider webhook route handling."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from anibridge.provider.base import Ref

from anibridge.app.exceptions import SchedulerNotInitializedError
from anibridge.app.web.routes.webhook import provider as webhook_module


class _BridgeClient:
    def __init__(self, result: tuple[bool, list[Ref] | None]) -> None:
        self.result = result
        self.requests: list[object] = []

    async def parse_webhook(self, request: object) -> tuple[bool, list[Ref] | None]:
        self.requests.append(request)
        return self.result


class _Scheduler:
    def __init__(self) -> None:
        self.bridge_clients: dict[str, _BridgeClient] = {}
        self.triggered: list[tuple[str, object, str]] = []

    def get_profiles_for_source_provider(self, _provider: str) -> list[str]:
        return ["valid", "invalid", "missing"]

    async def trigger_profile_sync(
        self,
        profile: str,
        *,
        request: object,
        source: str,
    ) -> None:
        self.triggered.append((profile, request, source))


@pytest.mark.asyncio
async def test_provider_webhook_requires_scheduler(monkeypatch) -> None:
    monkeypatch.setattr(
        webhook_module, "get_app_state", lambda: SimpleNamespace(scheduler=None)
    )

    with pytest.raises(SchedulerNotInitializedError):
        await webhook_module.provider_webhook.fn("plex", cast(Any, object()))


@pytest.mark.asyncio
async def test_provider_webhook_triggers_matching_profiles(monkeypatch) -> None:
    scheduler = _Scheduler()
    refs = [Ref.anchor("1"), Ref.anchor("2")]
    scheduler.bridge_clients = {
        "valid": _BridgeClient((True, refs)),
        "invalid": _BridgeClient((False, None)),
    }
    scheduled: list[tuple[str, Any]] = []

    def _schedule_task(coro, *, name: str) -> None:
        scheduled.append((name, coro))
        coro.close()

    monkeypatch.setattr(
        webhook_module,
        "get_app_state",
        lambda: SimpleNamespace(scheduler=scheduler),
    )
    monkeypatch.setattr(
        webhook_module,
        "_background_tasks",
        SimpleNamespace(create=_schedule_task),
    )

    request = cast(Any, object())
    await webhook_module.provider_webhook.fn("plex", request)

    assert scheduler.bridge_clients["valid"].requests == [request]
    assert scheduler.bridge_clients["invalid"].requests == [request]
    assert [name for name, _ in scheduled] == ["webhook_sync:valid"]
