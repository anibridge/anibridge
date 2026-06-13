"""Helpers for scheduling background tasks in the web layer."""

from collections.abc import Coroutine
from typing import Any

from anibridge.utils.tasks import schedule_task as schedule_shared_task

from anibridge.app.logging import get_logger

__all__ = ["schedule_task"]

log = get_logger(__name__)


def _on_task_error(name: str, _: Exception) -> None:
    """Log background task failures with web-specific context."""
    log.exception("Background task '%s' failed", name)


def schedule_task(coro: Coroutine[Any, Any, Any], *, name: str) -> None:
    """Schedule a coroutine in the background with error logging."""
    schedule_shared_task(coro, name=name, on_error=_on_task_error)
