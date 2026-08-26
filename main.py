"""AniBridge Main Application."""

import asyncio
import contextlib
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor

import uvicorn
from pydantic import ValidationError

from anibridge.app import ANIBDRIGE_HEADER, initialize_runtime
from anibridge.app.core.sched import SchedulerClient
from anibridge.app.logging import APP_LOGGER_NAME, configure_logging, get_logger
from anibridge.app.utils.terminal import supports_color
from anibridge.app.web.app import create_app
from anibridge.app.web.state import get_app_state

log = get_logger(APP_LOGGER_NAME)

_WILDCARD_HOSTS = {"", "0.0.0.0", "::", "[::]"}


def _warn_for_unauthenticated_public_bind(config) -> None:
    """Warn when the web UI is exposed broadly without authentication."""
    if config.web.host in _WILDCARD_HOSTS and not config.web.has_auth:
        log.warning(
            "Web UI is binding to all interfaces without authentication; "
            "configure Basic Auth or restrict web.host"
        )


async def _wait_for_runtime(
    scheduler: SchedulerClient,
    server_task: asyncio.Task | None,
) -> None:
    """Wait for scheduler completion while supervising the web server task."""
    if server_task is None:
        await scheduler.wait_for_completion()
        return

    scheduler_task = asyncio.create_task(scheduler.wait_for_completion())
    done, _ = await asyncio.wait(
        {scheduler_task, server_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if server_task in done:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task

        if server_task.cancelled():
            raise RuntimeError("Web server task was cancelled unexpectedly")
        server_error = server_task.exception()
        if server_error is not None:
            raise RuntimeError("Web server stopped unexpectedly") from server_error
        raise RuntimeError("Web server stopped unexpectedly")

    await scheduler_task


async def run() -> int:
    """Main application entry point.

    Initializes and runs the application scheduler until shutdown.

    Returns:
        int: Exit code (0 for success, 1 for error)
    """
    app_scheduler: SchedulerClient | None = None
    server_task: asyncio.Task | None = None
    server: uvicorn.Server | None = None

    ret = 0
    try:
        config = initialize_runtime()
        configure_logging(level=config.log_level, log_dir=config.data_path / "logs")

        loop = asyncio.get_running_loop()
        old_executor = loop._default_executor  # ty:ignore[unresolved-attribute]
        executor = ThreadPoolExecutor(max_workers=config.threads)
        loop.set_default_executor(executor)
        if old_executor is not None:
            old_executor.shutdown(wait=False)

        log.info("\n" + ANIBDRIGE_HEADER)

        if config.web.enabled:
            _warn_for_unauthenticated_public_bind(config)
            app = create_app()
            uv_config = uvicorn.Config(
                app,
                host=config.web.host,
                port=config.web.port,
                log_config=None,
                log_level="warning",
                access_log=False,
                loop="auto",  # uses uvloop if available, falls back to asyncio
                proxy_headers=True,
                forwarded_allow_ips="*",
            )

            server = uvicorn.Server(uv_config)
            # Use `_serve()` so uvicorn doesn't install its own signal handlers
            server_task = asyncio.create_task(server._serve())

            log.success(
                "Web UI started at "
                f"\033[92mhttp://{config.web.host}:{config.web.port} "
                "(ctrl+c to stop)\033[0m"
                if supports_color()
                else f"http://{config.web.host}:{config.web.port} (ctrl+c to stop)"
            )

        app_scheduler = SchedulerClient(config)
        await app_scheduler.initialize()
        await app_scheduler.start()
        get_app_state().set_scheduler(app_scheduler)

        if config.web.enabled:
            app.state.scheduler = app_scheduler

        _setup_signal_handlers_for_scheduler(app_scheduler)

        await _wait_for_runtime(app_scheduler, server_task)
    except KeyboardInterrupt:
        log.info("Keyboard interrupt received, shutting down")
    except ValidationError as e:
        log.error("Configuration validation error: %s", e)
        return 1
    except ConnectionError as e:
        log.error("Connection error: %s", e)
        return 1
    except (OSError, PermissionError) as e:
        log.error("File system error: %s", e)
        return 1
    except asyncio.CancelledError:
        log.info("Application cancelled")
        return 0
    except Exception as e:
        log.error("Unexpected application error: %s", e, exc_info=True)
        return 1
    finally:
        if app_scheduler:
            log.info("Shutting down application")
            try:
                await app_scheduler.stop()
                log.success("Application shutdown complete")
            except asyncio.CancelledError:
                log.info("Shutdown cancelled")
                ret = 1
            except Exception as e:
                log.error("Error during shutdown: %s", e, exc_info=True)
                ret = 1

        if server and server_task and not server_task.done():
            await _shutdown_web_server(server, server_task)

    app_state = get_app_state()
    if app_state.restart_requested:
        log.info("Restart requested, re-executing process")
        app_state.restart_requested = False
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except Exception as e:
            log.error("Failed to restart process: %s", e, exc_info=True)
            ret = 1

    return ret


def _setup_signal_handlers_for_scheduler(scheduler: SchedulerClient) -> None:
    """Install SIGINT/SIGTERM handlers that request scheduler shutdown."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()

    def _on_signal(sig):
        name = signal.Signals(sig).name if sig else "UNKNOWN"
        log.info("Received %s signal, initiating graceful shutdown", name)
        try:
            scheduler.request_shutdown()
        except Exception:
            log.debug(
                "Failed to request scheduler shutdown from signal handler",
                exc_info=True,
            )

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: _on_signal(s))
        except NotImplementedError:
            # Fallback for environments that don't support add_signal_handler
            signal.signal(sig, lambda s, f: _on_signal(s))


async def _shutdown_web_server(
    server: uvicorn.Server | None,
    server_task: asyncio.Task | None,
    *,
    timeout_duration: float = 10.0,
    force_timeout_duration: float = 5.0,
) -> None:
    """Attempt a graceful, then forced, shutdown of the web server."""
    if server is None or server_task is None:
        return

    server.should_exit = True
    try:
        async with asyncio.timeout(timeout_duration):
            await asyncio.shield(server_task)
        return
    except TimeoutError:
        log.warning(
            "Web server shutdown timed out after %.1fs; forcing exit",
            timeout_duration,
        )

    server.force_exit = True
    try:
        async with asyncio.timeout(force_timeout_duration):
            await asyncio.shield(server_task)
    except TimeoutError:
        log.error(
            "Forced web server shutdown timed out after %.1fs; cancelling server task",
            force_timeout_duration,
        )
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task


def main(argv: list[str] | None = None) -> int:
    """Main entry point.

    Initializes the application and runs the main event loop.

    Args:
        argv (list[str] | None): Command-line arguments (unused).

    Returns:
        int: Exit code (0 for success, 1 for error)
    """
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Application interrupted")
        return 0
    except Exception as e:
        log.error("Fatal error: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
