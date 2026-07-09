"""Litestar application factory and setup."""

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from logging import DEBUG
from pathlib import Path
from typing import Annotated

from litestar.app import Litestar
from litestar.config.compression import CompressionConfig
from litestar.connection.request import Request as LitestarRequest
from litestar.datastructures import CacheControlHeader
from litestar.enums import MediaType
from litestar.exceptions.http_exceptions import NotFoundException
from litestar.handlers.http_handlers.decorators import get
from litestar.middleware.base import ASGIMiddleware, DefineMiddleware
from litestar.middleware.logging import LoggingMiddlewareConfig
from litestar.openapi.config import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin
from litestar.params import PathParameter
from litestar.response.base import Response as LitestarResponse
from litestar.response.file import File
from litestar.router import Router
from litestar.static_files import create_static_files_router
from litestar.types.internal_types import ControllerRouterHandler

from anibridge.app import __version__
from anibridge.app.config.settings import get_config
from anibridge.app.core.sched.client import SchedulerClient
from anibridge.app.exceptions import AnibridgeError
from anibridge.app.logging import APP_LOGGER_NAME, attach_handler, get_logger
from anibridge.app.utils.paths import PROJECT_ROOT
from anibridge.app.web.middlewares.basic_auth import BasicAuthMiddleware
from anibridge.app.web.middlewares.request_logging import RequestLoggingMiddleware
from anibridge.app.web.routes.api import router as api_router
from anibridge.app.web.routes.webhook import router as webhook_router
from anibridge.app.web.routes.ws import router as ws_router
from anibridge.app.web.routes.z import router as z_router
from anibridge.app.web.services.history_service import get_history_service
from anibridge.app.web.services.logging_handler import get_log_ws_handler
from anibridge.app.web.state import get_app_state

__all__ = ["create_app"]

log = get_logger(APP_LOGGER_NAME)

FRONTEND_BUILD_DIR = PROJECT_ROOT / "frontend" / "build"
_ROOT_RELATIVE_URL_RE = re.compile(r'((?:href|src)=["\']|import\(")/')
_DEFAULT_ASSET_CACHE = CacheControlHeader(public=True, max_age=3600)
_IMMUTABLE_ASSET_CACHE = CacheControlHeader(
    public=True,
    max_age=31_536_000,
    immutable=True,
)


@asynccontextmanager
async def lifespan(app: Litestar) -> AsyncGenerator[None]:
    """Application lifespan context manager."""
    scheduler: SchedulerClient | None = getattr(app.state, "scheduler", None)
    if scheduler is None:
        log.debug("No scheduler passed; external lifecycle management expected")
    else:
        get_app_state().set_scheduler(scheduler)
        if not scheduler.is_running:
            await scheduler.initialize()
            await scheduler.start()
            log.success("Scheduler started for web UI")

    try:
        await get_app_state().ensure_public_anilist()
    except Exception:
        log.debug("Failed to initialize public AniList client at startup")

    purged_history_count = await get_history_service().purge_ephemeral_items()
    if purged_history_count:
        log.info(
            "Deleted %s ephemeral dry-run history entries at startup",
            purged_history_count,
        )

    log_ws_handler = get_log_ws_handler()
    attach_handler(log_ws_handler)
    try:
        loop = asyncio.get_running_loop()
        log_ws_handler.set_event_loop(loop)
    except Exception:
        pass
    try:
        yield
    finally:
        await get_app_state().shutdown()
        if scheduler and scheduler.is_running:
            await scheduler.stop()


def litestar_domain_exception_handler(
    request: LitestarRequest, exc: Exception
) -> LitestarResponse[dict[str, str]]:
    """Handle AniBridge errors inside the Litestar shell."""
    return LitestarResponse(
        content={
            "error": exc.__class__.__name__,
            "detail": str(exc) or exc.__class__.__doc__ or "",
            "path": request.url.path,
        },
        status_code=exc.status_code if isinstance(exc, AnibridgeError) else 500,
    )


def _render_frontend_spa(path_prefix: str) -> str:
    """Render the built SPA entrypoint."""
    html = (FRONTEND_BUILD_DIR / "index.html").read_text(encoding="utf-8")
    if not path_prefix:
        return html

    runtime_script = (
        f"<script>window.__ANIBRIDGE_PATH_PREFIX = {json.dumps(path_prefix)};</script>"
    )
    html = html.replace("</head>", f"        {runtime_script}\n    </head>", 1)
    html = _ROOT_RELATIVE_URL_RE.sub(rf"\1{path_prefix}/", html)
    return html.replace('base: ""', "base: window.__ANIBRIDGE_PATH_PREFIX", 1)


def _serve_frontend_asset(path: str) -> File:
    """Serve a static asset from the frontend build directory."""
    root_dir = FRONTEND_BUILD_DIR.resolve()
    file_path = (root_dir / path).resolve()
    try:
        file_path.relative_to(root_dir)
    except ValueError as exc:
        raise NotFoundException(
            "Asset path is outside the frontend build directory"
        ) from exc

    if not file_path.is_file():
        raise NotFoundException(f"Asset not found: {path}")

    normalized_path = path.lstrip("/")
    headers = {
        "cache-control": (
            "public, max-age=31536000, immutable"
            if normalized_path.startswith("_app/immutable/")
            else "public, max-age=3600"
        )
    }

    return File(
        path=file_path,
        filename=Path(path).name,
        content_disposition_type="inline",
        headers=headers,
    )


@get(path=["/", "/{path:path}"], include_in_schema=False)
async def serve_spa(
    path: Annotated[str, PathParameter()] = "",
) -> LitestarResponse[bytes] | LitestarResponse[str]:
    """Serve built frontend assets and fall back to the SPA entrypoint."""
    normalized_path = path.lstrip("/")
    path_prefix = get_config().web.path_prefix

    if (
        not normalized_path
        or normalized_path == "index.html"
        or not Path(normalized_path).suffix
    ):
        return LitestarResponse(
            content=_render_frontend_spa(path_prefix),
            media_type=MediaType.HTML,
            headers={"content-disposition": "inline", "cache-control": "no-cache"},
        )

    return _serve_frontend_asset(normalized_path)


def create_app(scheduler: SchedulerClient | None = None) -> Litestar:
    """Create the Litestar application."""
    config = get_config()
    middleware: list[ASGIMiddleware | DefineMiddleware] = []

    if log.getEffectiveLevel() <= DEBUG:
        middleware.append(
            LoggingMiddlewareConfig(
                logger_name=APP_LOGGER_NAME,
                middleware_class=RequestLoggingMiddleware,
                request_log_fields=("method", "path", "query", "body"),
                response_log_fields=("status_code",),
            ).middleware
        )
        log.debug("Request logging enabled in debug mode")

    if config.web.has_auth:
        middleware.append(
            DefineMiddleware(
                BasicAuthMiddleware,
                username=config.web.basic_auth.username,
                password=config.web.basic_auth.password.get_secret_value()
                if config.web.basic_auth.password
                else None,
                htpasswd_path=config.web.basic_auth.htpasswd_path,
                path_prefix=config.web.path_prefix,
                realm=config.web.basic_auth.realm,
            )
        )
        log.info("HTTP Basic Authentication enabled for web UI")

    route_handlers: list[ControllerRouterHandler] = [
        api_router,
        ws_router,
        webhook_router,
        z_router,
    ]

    if not FRONTEND_BUILD_DIR.exists():
        log.warning("Frontend build directory does not exist; no SPA will be served")
    elif not (FRONTEND_BUILD_DIR / "index.html").exists():
        log.error("Frontend index file does not exist; no SPA will be served")
    else:
        app_asset_dir = FRONTEND_BUILD_DIR / "_app"
        immutable_asset_dir = app_asset_dir / "immutable"

        if immutable_asset_dir.exists():
            route_handlers.append(
                create_static_files_router(
                    path="/_app/immutable",
                    directories=[immutable_asset_dir],
                    cache_control=_IMMUTABLE_ASSET_CACHE,
                    name="frontend-immutable-assets",
                )
            )

        if app_asset_dir.exists():
            route_handlers.append(
                create_static_files_router(
                    path="/_app",
                    directories=[app_asset_dir],
                    cache_control=_DEFAULT_ASSET_CACHE,
                    name="frontend-assets",
                )
            )

        route_handlers.append(serve_spa)

    if config.web.path_prefix:
        log.info("Serving AniBridge web UI under %s", config.web.path_prefix)
        route_handlers = [
            Router(path=config.web.path_prefix, route_handlers=route_handlers)
        ]

    app = Litestar(
        route_handlers=route_handlers,
        middleware=middleware,
        compression_config=CompressionConfig(backend="gzip"),
        lifespan=[lifespan],
        openapi_config=OpenAPIConfig(
            title="AniBridge",
            description="AniBridge web API.",
            version=__version__,
            use_handler_docstrings=True,
            path=(
                f"{config.web.path_prefix}/docs" if config.web.path_prefix else "/docs"
            ),
            render_plugins=[ScalarRenderPlugin()],
        ),
        exception_handlers={AnibridgeError: litestar_domain_exception_handler},
    )

    app.state.scheduler = scheduler
    return app
