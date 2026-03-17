"""Utilities for handling cron expressions and intervals."""

from typing import Annotated

from croniter import croniter
from pydantic import AfterValidator

__all__ = ["CronStr"]


def _validate_cron(value: str) -> str:
    """Validate a cron expression."""
    if not croniter.is_valid(value):
        raise ValueError(f"Invalid cron expression: {value}")
    return value


type CronStr = Annotated[str, AfterValidator(_validate_cron)]

