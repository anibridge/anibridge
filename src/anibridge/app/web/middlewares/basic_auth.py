"""Basic authentication middleware."""

import base64
import binascii
import secrets
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from anibridge.app import log
from anibridge.app.utils.htpasswd import HtpasswdFile

__all__ = ["BasicAuthMiddleware"]


class BasicAuthMiddleware:
    """Pure ASGI middleware that enforces HTTP Basic Authentication."""

    EXEMPT_PATHS = frozenset({"/healthz"})

    def __init__(
        self,
        app: ASGIApp,
        username: str | None = None,
        password: str | None = None,
        htpasswd_path: Path | None = None,
        realm: str = "AniBridge",
    ) -> None:
        """Initialize the BasicAuthMiddleware."""
        self.username = username
        self.password = password
        self.htpasswd_path = htpasswd_path
        self.realm = realm
        self.app = app
        self._htpasswd: HtpasswdFile | None = None
        self._htpasswd_mtime_ns: int | None = None

    def _validate_plain(self, username: str, password: str) -> bool:
        """Validate plain username and password credentials.

        Args:
            username (str): The provided username.
            password (str): The provided password.

        Returns:
            bool: True if credentials are valid, False otherwise.
        """
        username_match = (
            secrets.compare_digest(username, self.username)
            if self.username is not None
            else False
        )
        password_match = (
            secrets.compare_digest(password, self.password)
            if self.password is not None
            else False
        )
        return username_match and password_match

    def _load_htpasswd(self) -> HtpasswdFile | None:
        """Load the configured htpasswd file, reusing the parsed file when possible."""
        if not self.htpasswd_path:
            return None

        try:
            stat = self.htpasswd_path.stat()
        except FileNotFoundError:
            log.error("HTPasswd file not found at %s", self.htpasswd_path)
            self._htpasswd = None
            self._htpasswd_mtime_ns = None
            return None
        except OSError as e:
            log.exception("Error reading HTPasswd file metadata: %s", e)
            self._htpasswd = None
            self._htpasswd_mtime_ns = None
            return None

        if self._htpasswd and self._htpasswd_mtime_ns == stat.st_mtime_ns:
            return self._htpasswd

        try:
            self._htpasswd = HtpasswdFile.from_file(self.htpasswd_path)
            self._htpasswd_mtime_ns = stat.st_mtime_ns
        except Exception as e:
            log.exception("Error reading HTPasswd file: %s", e)
            self._htpasswd = None
            self._htpasswd_mtime_ns = None
            return None

        return self._htpasswd

    def _validate_htpasswd(self, username: str, password: str) -> bool:
        """Validate credentials against an htpasswd file.

        Args:
            username (str): The provided username.
            password (str): The provided password.

        Returns:
            bool: True if credentials are valid, False otherwise.
        """
        htpasswd = self._load_htpasswd()
        return htpasswd.check_password(username, password) if htpasswd else False

    def _extract_credentials(self, scope: Scope) -> tuple[str, str] | None:
        """Extract Basic Auth credentials from the request headers."""
        auth_header = Headers(scope=scope).get("authorization")
        if not auth_header:
            return None

        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "basic" or not token:
            return None

        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
        except binascii.Error, UnicodeDecodeError, ValueError:
            return None

        username, separator, password = decoded.partition(":")
        if not separator:
            return None
        return username, password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process the incoming request and enforce basic authentication."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        normalized_path = scope["path"].rstrip("/") or "/"
        if normalized_path in self.EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        credentials = self._extract_credentials(scope)
        if credentials:
            username, password = credentials
            if self._validate_plain(username, password) or self._validate_htpasswd(
                username, password
            ):
                await self.app(scope, receive, send)
                return

        await self._challenge_response()(scope, receive, send)

    def _challenge_response(self) -> Response:
        """Return a 401 response with the proper WWW-Authenticate header."""
        log.debug("Authentication failed, sending challenge response")
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{self.realm}"'},
        )
