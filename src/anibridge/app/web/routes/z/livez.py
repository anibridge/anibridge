"""Route for basic liveness check (not readiness)."""

from typing import Annotated, Literal

import msgspec
from litestar.handlers.http_handlers.decorators import get
from litestar.router import Router

__all__ = ["router"]


class LivezResponse(msgspec.Struct):
    """Minimal liveness payload for unauthenticated probes."""

    status: Annotated[
        Literal["ok"],
        msgspec.Meta(
            description="Fixed liveness status returned while the process is alive.",
            examples=["ok"],
        ),
    ] = "ok"


@get(path=["/livez", "/healthz"], include_in_schema=False)
async def livez() -> LivezResponse:
    """Liveness check endpoint."""
    return LivezResponse()


router = Router(path="", route_handlers=[livez])
