"""Tests for history websocket endpoint."""

from typing import cast

import pytest
from litestar.connection.websocket import WebSocket
from litestar.exceptions.websocket_exceptions import WebSocketDisconnect

from anibridge.app.web.routes.ws import history as history_ws_module


class _FakeHistoryWebSocket:
    def __init__(
        self,
        *,
        outcome: str | None = None,
        resource_kind: str | None = None,
    ) -> None:
        self.accepted = False
        self.closed = False
        self.messages: list[dict] = []
        self.query_params = {}
        if outcome is not None:
            self.query_params["outcome"] = outcome
        if resource_kind is not None:
            self.query_params["resource_kind"] = resource_kind

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_history_websocket_sends_latest_id_updates(monkeypatch) -> None:
    websocket = _FakeHistoryWebSocket(outcome="synced", resource_kind="record")

    class _Service:
        async def get_latest_id(
            self,
            *,
            profile: str,
            outcome: str | None,
            resource_kind: str | None,
        ):
            assert profile == "default"
            assert outcome == "synced"
            assert resource_kind == "record"
            return 42

    async def _disconnect(_seconds: float) -> None:
        raise WebSocketDisconnect(detail="disconnect event")

    monkeypatch.setattr(history_ws_module, "get_history_service", lambda: _Service())
    monkeypatch.setattr(history_ws_module.asyncio, "sleep", _disconnect)

    await history_ws_module.history_websocket.fn("default", cast(WebSocket, websocket))

    assert websocket.accepted is True
    assert websocket.messages == [
        {
            "profile": "default",
            "outcome": "synced",
            "resource_kind": "record",
            "latest_group_id": 42,
        }
    ]


@pytest.mark.asyncio
async def test_history_websocket_closes_on_unexpected_errors(monkeypatch) -> None:
    websocket = _FakeHistoryWebSocket()

    class _Service:
        async def get_latest_id(
            self,
            *,
            profile: str,
            outcome: str | None,
            resource_kind: str | None,
        ):
            raise RuntimeError("boom")

    monkeypatch.setattr(history_ws_module, "get_history_service", lambda: _Service())

    await history_ws_module.history_websocket.fn("default", cast(WebSocket, websocket))

    assert websocket.accepted is True
    assert websocket.closed is True
