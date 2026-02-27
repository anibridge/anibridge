"""Websocket endpoint for live logs."""

from fastapi.routing import APIRouter
from fastapi.websockets import WebSocket, WebSocketDisconnect

from anibridge.app.web.services.logging_handler import get_log_ws_handler

__all__ = ["router"]

router = APIRouter()


@router.websocket("")
async def logs_ws(ws: WebSocket) -> None:
    """Websocket endpoint for live logs.

    Args:
        ws (WebSocket): The WebSocket connection instance.
    """
    log_ws_handler = get_log_ws_handler()

    await ws.accept()
    await log_ws_handler.add(ws)
    try:
        while True:
            # Keep connection alive; we don't expect client messages
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await log_ws_handler.remove(ws)
