"""Websocket endpoint for periodic status snapshots."""

import asyncio

from fastapi.routing import APIRouter
from fastapi.websockets import WebSocket, WebSocketDisconnect

from anibridge.app.web.state import get_app_state

__all__ = ["router"]

router = APIRouter()


@router.websocket("")
async def status_ws(ws: WebSocket) -> None:
    """Websocket endpoint for periodic status snapshots.

    Args:
        ws (WebSocket): The WebSocket connection instance.
    """
    await ws.accept()
    try:
        while True:
            scheduler = get_app_state().scheduler
            data = (
                {"profiles": await scheduler.get_status()}
                if scheduler
                else {"profiles": {}}
            )
            await ws.send_json(data)

            # If any profile reports an active current_sync, increase refresh rate
            refresh = 5.0
            try:
                profiles = data.get("profiles", {})
                if any(
                    (p.get("status", {}).get("current_sync") or {}).get("state")
                    == "running"
                    for p in profiles.values()
                ):
                    refresh = 0.5
            except Exception:
                pass

            await asyncio.sleep(refresh)
    except WebSocketDisconnect:
        pass
