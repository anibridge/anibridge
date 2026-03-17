"""Generate human-readable strings."""

__all__ = ["human_duration"]


def human_duration(seconds: int) -> str:
    """Convert a duration in seconds to a human-readable string."""
    if seconds < 0:
        raise ValueError("Duration cannot be negative")
    parts = []
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)
