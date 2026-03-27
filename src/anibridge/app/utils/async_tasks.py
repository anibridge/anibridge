"""Helpers for scheduling background tasks in the web layer."""

from collections.abc import Coroutine
from typing import Any

from anibridge.utils.tasks import schedule_task as schedule_shared_task

from anibridge.app import log


def _on_task_error(name: str, _: Exception) -> None:
    """Log background task failures with web-specific context."""
    log.exception("Web - Background task '%s' failed", name)


def schedule_task(coro: Coroutine[Any, Any, Any], *, name: str) -> None:
    """Schedule a coroutine in the background with error logging.

    Args:
        coro (Coroutine[Any, Any, Any]): Coroutine to execute.
        name (str): Task name for logging context.
    """
    schedule_shared_task(coro, name=name, on_error=_on_task_error)
