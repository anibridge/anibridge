"""Tests for the websocket logging handler."""

import asyncio
import logging
from typing import Any, cast

import pytest

from anibridge.app.web.services.logging_handler import WebsocketLogHandler


class DummyWebSocket:
    """Simplified WebSocket implementation for handler tests."""

    def __init__(self) -> None:
        """Initialize the in-memory sink for websocket messages."""
        self.messages: list[dict[str, str | None]] = []
        self.closed = False

    async def send_json(self, payload):
        """Record outbound payloads for later inspection."""
        if self.closed:
            raise RuntimeError("connection closed")
        self.messages.append(payload)


@pytest.mark.asyncio
async def test_logging_handler_emits_inside_running_loop():
    """Emit broadcasts schedule tasks on the current loop when available."""
    handler = WebsocketLogHandler()
    ws = DummyWebSocket()
    await handler.add(cast(Any, ws))

    record = logging.LogRecord("test", logging.INFO, __file__, 0, "hello", None, None)
    handler.emit(record)
    await asyncio.sleep(0)

    assert ws.messages
    message = ws.messages[0]["message"]
    assert message is not None and message.endswith("hello")
    await handler.remove(cast(Any, ws))


def test_logging_handler_falls_back_to_threadsafe(monkeypatch: pytest.MonkeyPatch):
    """When off loop, emit uses run_coroutine_threadsafe to broadcast."""
    loop = asyncio.new_event_loop()
    handler = WebsocketLogHandler()
    handler.set_event_loop(loop)
    ws = DummyWebSocket()
    loop.run_until_complete(handler.add(cast(Any, ws)))

    def _raise_runtime_error():
        raise RuntimeError("no loop")

    monkeypatch.setattr("asyncio.get_running_loop", _raise_runtime_error)

    record = logging.LogRecord("test", logging.ERROR, __file__, 0, "boom", None, None)
    handler.emit(record)
    loop.run_until_complete(asyncio.sleep(0.01))
    assert ws.messages
    level = ws.messages[0]["level"]
    assert level == "ERROR"

    ws.closed = True
    handler.emit(record)
    loop.run_until_complete(asyncio.sleep(0.01))
    assert ws not in handler._connections
    loop.run_until_complete(handler.remove(cast(Any, ws)))
    loop.close()
