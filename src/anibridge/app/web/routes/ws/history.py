"""WebSocket endpoint for real-time history updates."""

import asyncio
from typing import Annotated

from litestar.connection.websocket import WebSocket
from litestar.exceptions.websocket_exceptions import WebSocketDisconnect
from litestar.handlers.websocket_handlers.route_handler import websocket
from litestar.params import PathParameter
from litestar.router import Router

from anibridge.app.web.services.history_service import get_history_service

__all__ = ["router"]


@websocket(path="/{profile:str}")
async def history_websocket(
    profile: Annotated[str, PathParameter()], socket: WebSocket
) -> None:
    """Stream live history updates to client.

    Polls for latest id and pushes only cursor updates when it changes.
    """
    await socket.accept()

    outcome = socket.query_params.get("outcome") or None
    resource_kind = socket.query_params.get("resource_kind") or None
    last_latest_id: int | None = None
    history_service = get_history_service()

    try:
        while True:
            latest_id = await history_service.get_latest_id(
                profile=profile, outcome=outcome, resource_kind=resource_kind
            )
            if latest_id != last_latest_id:
                last_latest_id = latest_id
                await socket.send_json(
                    {
                        "profile": profile,
                        "outcome": outcome,
                        "resource_kind": resource_kind,
                        "latest_group_id": latest_id,
                    }
                )

            await asyncio.sleep(3)

    except WebSocketDisconnect:
        pass
    except Exception:
        await socket.close()


router = Router(path="/history", route_handlers=[history_websocket])
