"""Webhook route aggregator."""

from fastapi.routing import APIRouter

from anibridge.app.web.routes.webhook.provider import router as provider_router

__all__ = ["router"]

router = APIRouter()
router.include_router(provider_router, prefix="")
