"""Websocket log broadcasting handler."""

import asyncio
import logging
import threading
from datetime import UTC, datetime
from typing import Protocol

from anibridge.utils.cache import cache

from anibridge.app.logging import get_logger

__all__ = ["WebsocketLogHandler", "get_log_ws_handler"]

log = get_logger(__name__)


class LogSocket(Protocol):
    """Minimal websocket interface required for log streaming."""

    async def send_json(self, data: dict[str, str | None], /) -> None:
        """Send a JSON payload to the connected websocket client."""


class WebsocketLogHandler(logging.Handler):
    """Logging handler that broadcasts log records to active websocket clients."""

    def __init__(self) -> None:
        """Initialize the WebsocketLogHandler."""
        super().__init__()
        self._connections: set[LogSocket] = set()
        self._lock = threading.RLock()
        self._tasks: set[asyncio.Task[None]] = set()  # Prevents early GC
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the main event loop to schedule tasks from other threads."""
        self._loop = loop

    async def add(self, ws: LogSocket) -> None:
        """Add a websocket connection to the handler."""
        with self._lock:
            self._connections.add(ws)
        log.debug("Client added (%s total)", len(self._connections))

    async def remove(self, ws: LogSocket) -> None:
        """Remove a websocket connection from the handler."""
        with self._lock:
            self._connections.discard(ws)
        log.debug("Client removed (%s total)", len(self._connections))

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record to all connected websocket clients."""
        try:
            msg = self.format(record)
        except Exception:
            return

        with self._lock:
            conns = tuple(self._connections)

        if not conns:
            return

        try:
            current_loop = asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            current_loop = None
            in_loop = False

        if in_loop and current_loop is not None:
            for ws in conns:
                task = current_loop.create_task(
                    self._safe_send(ws, msg, record.levelname, record.created)
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return

        if self._loop and not self._loop.is_closed():
            for ws in conns:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._safe_send(ws, msg, record.levelname, record.created),
                        self._loop,
                    )
                except Exception:
                    continue
            return

    async def _safe_send(
        self, ws: LogSocket, msg: str, level: str, created: float | None
    ) -> None:
        """Send a message to a websocket connection."""
        try:
            timestamp = None
            if created is not None:
                try:
                    timestamp = datetime.fromtimestamp(created, tz=UTC).isoformat()
                except Exception:
                    timestamp = None

            await ws.send_json({"level": level, "message": msg, "timestamp": timestamp})
        except Exception:
            await self.remove(ws)


@cache
def get_log_ws_handler() -> WebsocketLogHandler:
    """Get the singleton WebsocketLogHandler instance."""
    return WebsocketLogHandler()
