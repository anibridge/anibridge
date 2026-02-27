"""AniBridge Main Application."""

import asyncio
import os
import signal
import sys

import uvicorn
from pydantic import ValidationError

from anibridge.app import ANIBDRIGE_HEADER, log
from anibridge.app.config.settings import get_config
from anibridge.app.core.sched import SchedulerClient
from anibridge.app.web.app import create_app
from anibridge.app.web.state import get_app_state


def _setup_signal_handlers_for_scheduler(scheduler: SchedulerClient) -> None:
    """Install SIGINT/SIGTERM handlers that request scheduler shutdown."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()

    def _on_signal(sig):
        name = signal.Signals(sig).name if sig else "UNKNOWN"
        log.info(f"AniBridge - Received {name} signal, initiating graceful shutdown...")
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


def validate_configuration():
    """Validate the application configuration and display profile information.

    Returns:
        bool: True if configuration is valid, False otherwise
    """
    config = get_config()

    def _profile_error(profile_name: str) -> str | None:
        try:
            profile_config = config.get_profile(profile_name)
            log.info(f"AniBridge - Profile $$'{profile_name}'$$: {profile_config!s}")
        except KeyError as e:
            return f"AniBridge - Profile $$'{profile_name}'$$ not found: {e}"
        except ValidationError as e:
            return (
                f"AniBridge - Invalid configuration for profile "
                f"$$'{profile_name}'$$: {e}"
            )
        except ValueError as e:
            return (
                f"AniBridge - Configuration error for profile $$'{profile_name}'$$: {e}"
            )
        except (AttributeError, TypeError) as e:
            return (
                f"AniBridge - Configuration structure error for profile "
                f"$$'{profile_name}'$$: {e}"
            )
        return None

    try:
        if len(config.profiles) == 0:
            log.warning("AniBridge - No sync profiles configured")
            return True

        errors = [
            message
            for profile_name in config.profiles
            if (message := _profile_error(profile_name)) is not None
        ]

        for message in errors:
            log.error(message)

        return not errors
    except ValidationError as e:
        log.error(f"AniBridge - Global configuration validation failed: {e}")
        return False
    except ValueError as e:
        log.error(f"AniBridge - Configuration value error: {e}")
        return False
    except (OSError, PermissionError) as e:
        log.error(f"AniBridge - File system error during configuration: {e}")
        return False
    except Exception as e:
        log.error(f"AniBridge - Unexpected configuration error: {e}", exc_info=True)
        return False


async def run() -> int:
    """Main application entry point.

    Initializes and runs the application scheduler until shutdown.

    Returns:
        int: Exit code (0 for success, 1 for error)
    """
    app_scheduler: SchedulerClient | None = None
    server_task: asyncio.Task | None = None
    config = get_config()

    ret = 0
    try:
        log.info("\n" + ANIBDRIGE_HEADER)

        if not validate_configuration():
            return 1

        app_scheduler = SchedulerClient(config)
        await app_scheduler.initialize()
        await app_scheduler.start()

        _setup_signal_handlers_for_scheduler(app_scheduler)

        if config.web.enabled:
            app = create_app(app_scheduler)
            uv_config = uvicorn.Config(
                app,
                host=config.web.host,
                port=config.web.port,
                log_config=None,
                loop="asyncio",
                proxy_headers=True,
                forwarded_allow_ips="*",
            )

            server = uvicorn.Server(uv_config)
            # Use `_serve()` so uvicorn doesn't install its own signal handlers
            server_task = asyncio.create_task(server._serve())

            log.success(
                "AniBridge - Web UI started at "
                f"\033[92mhttp://{config.web.host}:{config.web.port} "
                "(ctrl+c to stop)\033[0m"
            )

            await app_scheduler.wait_for_completion()

            # Signal uvicorn server to stop and wait for it
            server.should_exit = True
            await server_task
        else:
            await app_scheduler.wait_for_completion()
    except KeyboardInterrupt:
        log.info("AniBridge - Keyboard interrupt received, shutting down...")
    except ValidationError as e:
        log.error(f"AniBridge - Configuration validation error: {e}")
        return 1
    except ConnectionError as e:
        log.error(f"AniBridge - Connection error: {e}")
        return 1
    except (OSError, PermissionError) as e:
        log.error(f"AniBridge - File system error: {e}")
        return 1
    except asyncio.CancelledError:
        log.info("AniBridge - Application cancelled")
        return 0
    except Exception as e:
        log.error(f"AniBridge - Unexpected application error: {e}", exc_info=True)
        return 1
    finally:
        if app_scheduler:
            log.info("AniBridge - Shutting down application...")
            try:
                await app_scheduler.stop()
                log.success("AniBridge - Application shutdown complete")
            except asyncio.CancelledError:
                log.info("AniBridge - Shutdown cancelled")
                ret = 1
            except Exception as e:
                log.error(f"AniBridge - Error during shutdown: {e}", exc_info=True)
                ret = 1

    app_state = get_app_state()
    if app_state.restart_requested:
        log.info("AniBridge - Restart requested, re-executing process...")
        app_state.restart_requested = False
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except Exception as e:
            log.error(f"AniBridge - Failed to restart process: {e}", exc_info=True)
            ret = 1

    return ret


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
        log.info("AniBridge - Application interrupted")
        return 0
    except Exception as e:
        log.error(f"AniBridge - Fatal error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
