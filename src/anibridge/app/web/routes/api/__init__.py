"""API routes."""

from fastapi.routing import APIRouter

from anibridge.app.web.routes.api.backups import router as backups_router
from anibridge.app.web.routes.api.config import router as config_router
from anibridge.app.web.routes.api.history import router as history_router
from anibridge.app.web.routes.api.logs import router as logs_history_router
from anibridge.app.web.routes.api.mappings import router as mappings_router
from anibridge.app.web.routes.api.pins import router as pins_router
from anibridge.app.web.routes.api.status import router as status_router
from anibridge.app.web.routes.api.sync import router as sync_router
from anibridge.app.web.routes.api.system import router as system_router

__all__ = ["router"]

router = APIRouter()


router.include_router(history_router, prefix="/history", tags=["history"])
router.include_router(backups_router, prefix="/backups", tags=["backups"])
router.include_router(mappings_router, prefix="/mappings", tags=["mappings"])
router.include_router(pins_router, prefix="/pins", tags=["pins"])
router.include_router(logs_history_router, prefix="/logs", tags=["logs"])
router.include_router(status_router, prefix="/status", tags=["status"])
router.include_router(sync_router, prefix="/sync", tags=["sync"])
router.include_router(system_router, prefix="/system", tags=["system"])
router.include_router(config_router, prefix="/config", tags=["config"])
