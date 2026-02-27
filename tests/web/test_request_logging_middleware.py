"""Tests for the request logging middleware."""

from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from anibridge.app.web.middlewares import request_logging
from anibridge.app.web.middlewares.request_logging import RequestLoggingMiddleware


class _DummyLogger:
    """Simple logger that captures debug messages for assertions."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, message: str, *args, **kwargs) -> None:
        if args:
            message = message % args
        self.messages.append(message)


async def _noop_app(scope: Scope, receive: Receive, send: Send) -> None:
    """No-op ASGI app; required to satisfy BaseHTTPMiddleware."""
    return None


def _make_request(
    method: str = "GET",
    path: str = "/",
    query: str = "",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> Request:
    """Create a Starlette Request suitable for exercising the middleware."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "scheme": "http",
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "query_string": query.encode(),
        "headers": [
            (name.lower().encode("utf-8"), value.encode("utf-8"))
            for name, value in (headers or {}).items()
        ],
    }

    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if body_sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        body_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_request_logging_middleware_logs_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Middleware logs method, path, query, and status for successful requests."""
    logger = _DummyLogger()
    monkeypatch.setattr(request_logging, "log", logger)

    middleware = RequestLoggingMiddleware(_noop_app)

    request = _make_request(path="/status", query="ready=true")

    async def call_next(req: Request) -> Response:
        return Response(content=b"ok", status_code=204)

    response = await middleware.dispatch(request, call_next)

    assert response.status_code == 204
    assert logger.messages, "expected debug output"
    assert "GET /status?ready=true" in logger.messages[0]
    assert "Response: 204" in logger.messages[0]


@pytest.mark.asyncio
async def test_request_logging_middleware_captures_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Middleware captures readable request bodies without consuming the stream."""
    logger = _DummyLogger()
    monkeypatch.setattr(request_logging, "log", logger)

    middleware = RequestLoggingMiddleware(_noop_app)

    json_body = b'{"message":"hello"}'
    request = _make_request(
        method="POST",
        path="/api/items",
        body=json_body,
        headers={"content-type": "application/json"},
    )

    async def call_next(req: Request) -> Response:
        # Ensure downstream handler can still access the body.
        assert await req.body() == json_body
        return Response(status_code=201)

    response = await middleware.dispatch(request, call_next)

    assert response.status_code == 201
    assert any('Body: {"message":"hello"}' in msg for msg in logger.messages)
    stored_body = request.scope.get("body")
    assert stored_body is not None
    assert stored_body.getvalue() == json_body


@pytest.mark.asyncio
async def test_request_logging_middleware_logs_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Middleware logs failures and re-raises downstream exceptions."""
    logger = _DummyLogger()
    monkeypatch.setattr(request_logging, "log", logger)

    middleware = RequestLoggingMiddleware(_noop_app)
    request = _make_request(method="DELETE", path="/api/items/42")

    async def failing_call_next(_: Request) -> Response:
        # Exercise the middleware's failure logging path.
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await middleware.dispatch(request, failing_call_next)

    assert any("Failed" in message for message in logger.messages)


@pytest.mark.asyncio
async def test_request_logging_middleware_truncates_long_text_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large text payloads are truncated to avoid oversized log entries."""
    logger = _DummyLogger()
    monkeypatch.setattr(request_logging, "log", logger)

    middleware = RequestLoggingMiddleware(_noop_app)

    long_body = ("x" * 1200).encode()
    request = _make_request(
        method="POST",
        path="/big",
        body=long_body,
        headers={"content-type": "application/json"},
    )

    async def call_next(req: Request) -> Response:
        assert len(await req.body()) == len(long_body)
        return Response(status_code=202)

    await middleware.dispatch(request, call_next)

    assert any("..." in message and "Body:" in message for message in logger.messages)


@pytest.mark.asyncio
async def test_request_logging_middleware_handles_binary_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binary payloads are logged with metadata instead of decoded text."""
    logger = _DummyLogger()
    monkeypatch.setattr(request_logging, "log", logger)

    middleware = RequestLoggingMiddleware(_noop_app)

    request = _make_request(
        method="POST",
        path="/upload",
        body=b"\x00\x01",
        headers={"content-type": "application/octet-stream"},
    )

    async def call_next(req: Request) -> Response:
        assert await req.body() == b"\x00\x01"
        return Response(status_code=200)

    await middleware.dispatch(request, call_next)

    assert any("<application/octet-stream, 2 bytes>" in msg for msg in logger.messages)


@pytest.mark.asyncio
async def test_request_logging_middleware_handles_decode_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UnicodeDecodeError branches fall back to binary metadata logging."""
    logger = _DummyLogger()
    monkeypatch.setattr(request_logging, "log", logger)

    middleware = RequestLoggingMiddleware(_noop_app)

    bad_body = b"\xff\xfe"
    request = _make_request(
        method="POST",
        path="/garbled",
        body=bad_body,
        headers={"content-type": "text/plain"},
    )

    async def call_next(req: Request) -> Response:
        assert await req.body() == bad_body
        return Response(status_code=200)

    await middleware.dispatch(request, call_next)

    assert any("binary data" in message for message in logger.messages)


@pytest.mark.asyncio
async def test_request_logging_middleware_handles_body_read_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceptions while reading the body are logged and do not break the request."""
    logger = _DummyLogger()
    monkeypatch.setattr(request_logging, "log", logger)

    middleware = RequestLoggingMiddleware(_noop_app)
    request = _make_request(method="POST", path="/broken")

    async def broken_body() -> bytes:
        raise RuntimeError("boom")

    request.body = broken_body  # type: ignore[assignment]

    async def call_next(req: Request) -> Response:
        return Response(status_code=204)

    response = await middleware.dispatch(request, call_next)
    assert response.status_code == 204
    assert any("<error reading" in message for message in logger.messages)
