"""Helpers for scheduling background tasks in the web layer."""

import asyncio
from collections.abc import Coroutine
from typing import Any

from anibridge.app import log

_background_tasks = set()


async def _run_task(coro: Coroutine[Any, Any, Any], *, name: str) -> None:
    """Run a coroutine and log failures.

    Args:
        coro (Coroutine[Any, Any, Any]): Coroutine to execute.
        name (str): Task name for logging context.
    """
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Web - Background task '%s' failed", name)


def schedule_task(coro: Coroutine[Any, Any, Any], *, name: str) -> None:
    """Schedule a coroutine in the background with error logging.

    Args:
        coro (Coroutine[Any, Any, Any]): Coroutine to execute.
        name (str): Task name for logging context.
    """
    task = asyncio.create_task(_run_task(coro, name=name))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
