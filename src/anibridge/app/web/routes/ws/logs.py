"""Websocket endpoint for live logs."""

from litestar.connection.websocket import WebSocket
from litestar.exceptions.websocket_exceptions import WebSocketDisconnect
from litestar.handlers.websocket_handlers.route_handler import websocket
from litestar.router import Router

from anibridge.app.web.services.logging_handler import get_log_ws_handler

__all__ = ["router"]


@websocket(path="")
async def logs_ws(socket: WebSocket) -> None:
    """Websocket endpoint for live logs."""
    log_ws_handler = get_log_ws_handler()

    await socket.accept()
    await log_ws_handler.add(socket)
    try:
        while True:
            # Keep connection alive; we don't expect client messages
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await log_ws_handler.remove(socket)


router = Router(path="/logs", route_handlers=[logs_ws])
