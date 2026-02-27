"""WebSocket endpoint for real-time timeline updates."""

import asyncio

from fastapi.routing import APIRouter
from fastapi.websockets import WebSocket, WebSocketDisconnect

from anibridge.app.web.services.history_service import get_history_service

__all__ = ["router"]

router = APIRouter()


@router.websocket("/{profile}")
async def history_websocket(websocket: WebSocket, profile: str) -> None:
    """Stream live history updates to client.

    Polls for changes every 5 seconds and pushes updates when items change.
    """
    await websocket.accept()

    last_item_ids: set[int] = set()

    try:
        while True:
            page_data = await get_history_service().get_page(
                profile=profile, page=1, per_page=25, outcome=None
            )

            # Check if items have changed
            current_ids = {item.id for item in page_data.items}

            if current_ids != last_item_ids:
                last_item_ids = current_ids

                await websocket.send_json(
                    {
                        "items": [
                            item.model_dump(mode="json") for item in page_data.items
                        ],
                        "stats": page_data.stats,
                        "profile": profile,
                        "total": page_data.total,
                    }
                )

            await asyncio.sleep(5)

    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.close()
