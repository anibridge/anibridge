"""FastAPI application factory and setup."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from logging import DEBUG
from pathlib import Path
from typing import Any, cast

from fastapi.applications import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from anibridge.app import __version__, config, log
from anibridge.app.core.sched import SchedulerClient
from anibridge.app.exceptions import AniBridgeError
from anibridge.app.web.middlewares.basic_auth import BasicAuthMiddleware
from anibridge.app.web.middlewares.request_logging import RequestLoggingMiddleware
from anibridge.app.web.routes import router
from anibridge.app.web.services.logging_handler import get_log_ws_handler
from anibridge.app.web.state import get_app_state

__all__ = ["create_app"]

FRONTEND_BUILD_DIR = Path(__file__).parent.parent.parent / "frontend" / "build"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """Application lifespan context manager.

    Args:
        app (FastAPI): The FastAPI application instance.

    Returns:
        AsyncGenerator: The application lifespan context manager.
    """
    scheduler: SchedulerClient | None = app.extra.get("scheduler")
    if scheduler is None:
        log.info("Web - No scheduler passed; external lifecycle management expected")
    else:
        get_app_state().set_scheduler(scheduler)
        if not scheduler._running:
            await scheduler.initialize()
            await scheduler.start()
            log.success("Web - Scheduler started for web UI")

    try:
        await get_app_state().ensure_public_anilist()
    except Exception:
        log.debug("Web - Failed to initialize public AniList client at startup")

    root_logger = log
    log_ws_handler = get_log_ws_handler()
    if log_ws_handler not in root_logger.handlers:
        root_logger.addHandler(log_ws_handler)
    try:
        loop = asyncio.get_running_loop()
        log_ws_handler.set_event_loop(loop)
    except Exception:
        pass
    try:
        yield
    finally:
        await get_app_state().shutdown()
        if scheduler and scheduler._running:
            await scheduler.stop()


def create_app(scheduler: SchedulerClient | None = None) -> FastAPI:
    """Create the FastAPI application.

    Args:
        scheduler (SchedulerClient | None): The scheduler client instance.

    Returns:
        FastAPI: The created FastAPI application.
    """
    app = FastAPI(title="AniBridge", lifespan=lifespan, version=__version__)

    if scheduler:
        app.extra["scheduler"] = scheduler

    # Add request logging middleware if in debug mode
    if log.level <= DEBUG:
        app.add_middleware(cast(Any, RequestLoggingMiddleware))
        log.debug("Web - Request logging enabled (debug mode)")

    # Add basic auth middleware if configured
    if config.web.has_auth:
        app.add_middleware(
            cast(Any, BasicAuthMiddleware),
            username=config.web.basic_auth.username,
            password=config.web.basic_auth.password.get_secret_value()
            if config.web.basic_auth.password
            else None,
            htpasswd_path=config.web.basic_auth.htpasswd_path,
            realm=config.web.basic_auth.realm,
        )
        log.info("Web - HTTP Basic Authentication enabled for web UI")

    app.include_router(router)

    index_file = FRONTEND_BUILD_DIR / "index.html"
    if not FRONTEND_BUILD_DIR.exists():
        log.warning(
            "Web - Frontend build directory does not exist, no SPA will be served"
        )
        return app
    if not index_file.exists():
        log.error("Web - Frontend index file does not exist, no SPA will be served")
        return app

    app.mount("/", StaticFiles(directory=FRONTEND_BUILD_DIR, html=True), name="spa")

    api_prefixes = ("/api/", "/ws/", "/webhook/")

    @app.exception_handler(StarletteHTTPException)
    async def spa_404_handler(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        """Serve SPA index.html for unknown routes.

        Args:
            request (Request): The incoming HTTP request.
            exc (StarletteHTTPException): The exception instance.

        Returns:
            Response: The response to return.
        """
        if (
            exc.status_code == 404
            and not request.url.path.startswith(api_prefixes)
            and "." not in request.url.path.rsplit("/", 1)[-1]
        ):
            return FileResponse(index_file)
        return await http_exception_handler(request, exc)

    @app.exception_handler(AniBridgeError)
    def domain_exception_handler(request: Request, exc: AniBridgeError) -> JSONResponse:
        """Handle AniBridge errors with structured JSON responses.

        Args:
            request (Request): The incoming HTTP request.
            exc (AniBridgeError): The exception instance.

        Returns:
            JSONResponse: Structured JSON response with error details.
        """
        cls = exc.__class__
        payload = {
            "error": cls.__name__,
            "detail": str(exc) or cls.__doc__ or "",
            "path": request.url.path,
        }
        return JSONResponse(status_code=cls.status_code, content=payload)

    return app
