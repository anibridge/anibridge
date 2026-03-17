"""Utilities for handling cron expressions and intervals."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from croniter import croniter
from pydantic import AfterValidator

__all__ = [
    "CronStr",
    "format_interval",
    "get_next_interval_seconds",
    "get_next_run_datetime",
    "is_enabled_interval",
]


def _validate_cron(value: str) -> str:
    """Validate a cron expression."""
    if not croniter.is_valid(value):
        raise ValueError(f"Invalid cron expression: {value}")
    return value


CronStr = Annotated[str, AfterValidator(_validate_cron)]


def get_next_run_datetime(interval: int | str, now: datetime | None = None) -> datetime:
    """Get the datetime of the next interval trigger.

    Args:
        interval: Either an integer (seconds) or a cron expression string.
        now: Optional datetime to compute from (defaults to now).

    Returns:
        datetime: The datetime of the next trigger.
    """
    base = now or datetime.now(tz=UTC)
    if isinstance(interval, int):
        if interval <= 0:
            raise ValueError("Integer interval must be greater than 0")
        return base + timedelta(seconds=interval)
    if isinstance(interval, str):
        if not croniter.is_valid(interval):
            raise ValueError(f"Invalid cron expression: {interval}")
        return croniter(interval, base).get_next(datetime)
    raise ValueError(f"Invalid interval type: {type(interval)}")


def get_next_interval_seconds(interval: int | str, now: datetime | None = None) -> int:
    """Get the seconds to wait until the next interval trigger.

    Args:
        interval: Either an integer (seconds) or a cron expression string.
        now: Optional datetime to compute from (defaults to now).

    Returns:
        int: Seconds to wait until the next interval trigger.

    Raises:
        ValueError: If interval is invalid.
    """
    base = now or datetime.now()
    next_run = get_next_run_datetime(interval, base)
    return int((next_run - base).total_seconds())


def format_interval(interval: int | str) -> str:
    """Format an interval for display/logging.

    Args:
        interval: Either an integer (seconds) or a cron expression string.

    Returns:
        str: Human-readable interval description.
    """
    if isinstance(interval, int):
        return f"{interval}s"
    if isinstance(interval, str):
        return f"cron({interval})"
    return str(interval)


def is_enabled_interval(interval: int | str) -> bool:
    """Check if an interval is enabled (non-zero).

    Args:
        interval: Either an integer (seconds) or a cron expression string.

    Returns:
        bool: True if the interval is enabled.
    """
    if isinstance(interval, int):
        return interval > 0
    if isinstance(interval, str):
        return croniter.is_valid(interval)
    return False
