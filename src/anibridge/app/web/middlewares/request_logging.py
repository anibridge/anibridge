"""Middlewares for handling requests and responses."""

import time
from collections.abc import Awaitable, Callable
from io import BytesIO

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from anibridge.app import log
from anibridge.app.utils.terminal import ARROW

__all__ = ["RequestLoggingMiddleware"]


class RequestLoggingMiddleware:
    """Pure ASGI middleware that logs incoming requests and responses."""

    BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

    def __init__(self, app: ASGIApp) -> None:
        """Initialize the middleware."""
        self.app = app

    def _build_request_info(self, request: Request) -> str:
        """Build the request portion of the debug log entry."""
        return (
            f"{request.method} {request.url.path}"
            f"{f'?{request.url.query}' if request.url.query else ''} "
            f"from {request.client.host if request.client else 'unknown'}"
        )

    def _format_body_info(self, body: bytes, content_type: str) -> str:
        """Format body content for readable debug logging."""
        if not body:
            return ""

        if "application/json" in content_type or "text/" in content_type:
            try:
                body_str = body.decode("utf-8")
            except UnicodeDecodeError:
                return f" Body: <binary data, {len(body)} bytes>"

            if len(body_str) > 1000:
                body_str = body_str[:1000] + "..."
            return f" Body: {body_str}"

        return f" Body: <{content_type or 'unknown'}, {len(body)} bytes>"

    async def _capture_body(self, request: Request) -> str:
        """Capture the request body without breaking downstream access."""
        if request.method not in self.BODY_METHODS:
            return ""

        try:
            body = await request.body()
        except Exception as e:
            return f" Body: <error reading: {e}>"

        request._body = body
        request.scope["body"] = BytesIO(body)
        content_type = request.headers.get("content-type", "").lower()
        return self._format_body_info(body, content_type)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Process a request for unit tests and direct middleware exercise."""
        start_time = time.perf_counter()
        request_info = self._build_request_info(request)
        body_info = await self._capture_body(request)
        full_request_info = request_info + body_info

        try:
            response = await call_next(request)
            process_time = time.perf_counter() - start_time

            log.debug(
                "Request: %s %s Response: %s (%.3fs)",
                full_request_info,
                ARROW,
                response.status_code,
                process_time,
            )

            return response
        except Exception:
            process_time = time.perf_counter() - start_time
            log.debug(
                "Request: %s %s Failed (%.3fs)",
                full_request_info,
                ARROW,
                process_time,
            )
            raise

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process the incoming ASGI request and log the result."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = time.perf_counter()
        request = Request(scope, receive)
        full_request_info = self._build_request_info(request)
        replay_receive = receive

        if request.method in self.BODY_METHODS:
            body_info = await self._capture_body(request)
            full_request_info += body_info
            body = getattr(request, "_body", None)
            if body is not None:
                replay_receive = self._build_body_replay_receive(body)

        status_code = 500

        async def send_with_logging(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, replay_receive, send_with_logging)
            process_time = time.perf_counter() - start_time
            log.debug(
                "Request: %s %s Response: %s (%.3fs)",
                full_request_info,
                ARROW,
                status_code,
                process_time,
            )
        except Exception:
            process_time = time.perf_counter() - start_time
            log.debug(
                "Request: %s %s Failed (%.3fs)",
                full_request_info,
                ARROW,
                process_time,
            )
            raise

    @staticmethod
    def _build_body_replay_receive(body: bytes) -> Receive:
        """Create a receive callable that replays a buffered request body."""
        body_sent = False

        async def replay_receive() -> Message:
            nonlocal body_sent
            if body_sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return replay_receive
