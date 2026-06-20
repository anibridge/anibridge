"""Tests for request logging middleware customization."""

from typing import Any, cast

from anibridge.app.web.middlewares.request_logging import RequestLoggingMiddleware


class FakeLogger:
    """Logger double that records debug calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def debug(self, message: str, *args: object, **kwargs: object) -> None:
        if args:
            message = message % args
        self.calls.append((message, kwargs))


def _middleware(
    *,
    is_struct_logger: bool,
) -> tuple[RequestLoggingMiddleware, FakeLogger]:
    middleware = RequestLoggingMiddleware.__new__(RequestLoggingMiddleware)
    middleware.is_struct_logger = is_struct_logger
    logger = FakeLogger()
    cast(Any, middleware).logger = logger
    return middleware, logger


def test_request_logging_middleware_logs_structured_values() -> None:
    """Structured loggers should receive remaining values as keyword args."""
    middleware, logger = _middleware(is_struct_logger=True)

    middleware.log_message({"message": "handled", "status_code": 200})

    assert logger.calls == [("handled", {"status_code": 200})]


def test_request_logging_middleware_formats_standard_logger_values() -> None:
    """Standard loggers should receive a single formatted debug message."""
    middleware, logger = _middleware(is_struct_logger=False)

    middleware.log_message({"message": "handled", "status_code": 200, "path": "/"})

    assert logger.calls == [
        ("handled: status_code=200, path=/", {}),
    ]
