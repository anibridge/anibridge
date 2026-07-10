"""Websocket endpoint for periodic status snapshots."""

import asyncio

from litestar.connection.websocket import WebSocket
from litestar.exceptions.websocket_exceptions import WebSocketDisconnect
from litestar.handlers.websocket_handlers.route_handler import websocket
from litestar.router import Router

from anibridge.app.web.state import get_app_state

__all__ = ["router"]

_MAX_IDLE_INTERVAL = 10.0
_ACTIVE_SYNC_INTERVAL = 0.5


@websocket(path="")
async def status_ws(socket: WebSocket) -> None:
    """Websocket endpoint for periodic status snapshots."""
    await socket.accept()
    app_state = get_app_state()
    try:
        while True:
            scheduler = app_state.scheduler
            data = (
                {"profiles": await scheduler.get_status()}
                if scheduler
                else {"profiles": {}}
            )
            await socket.send_json(data)

            # If any profile reports an active current_sync, use a fast refresh.
            # Otherwise, wait for an explicit status-change notification (or timeout).
            try:
                profiles = data.get("profiles", {})
                syncing = any(
                    (p.get("status", {}).get("current_sync") or {}).get("state")
                    == "running"
                    for p in profiles.values()
                )
            except Exception:
                syncing = False

            if syncing:
                await asyncio.sleep(_ACTIVE_SYNC_INTERVAL)
            else:
                await app_state.wait_status_change(max_wait=_MAX_IDLE_INTERVAL)
    except WebSocketDisconnect:
        pass


router = Router(path="/status", route_handlers=[status_ws])
