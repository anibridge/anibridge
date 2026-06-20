"""Middlewares for handling requests and responses."""

from litestar.middleware.logging import LoggingMiddleware

__all__ = ["RequestLoggingMiddleware"]


class RequestLoggingMiddleware(LoggingMiddleware):
    """Litestar logging middleware variant that emits debug-level records."""

    def log_message(self, values: dict[str, object]) -> None:
        """Log request and response messages at debug level instead of info."""
        message = str(values.pop("message"))
        if self.is_struct_logger:
            self.logger.debug(message, **values)
            return

        value_strings = [f"{key}={value}" for key, value in values.items()]
        self.logger.debug("%s: %s", message, ", ".join(value_strings))
