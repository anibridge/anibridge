"""Basic authentication middleware."""

import base64
import binascii
import secrets
from pathlib import Path
from typing import ClassVar

from litestar.connection.base import ASGIConnection
from litestar.datastructures.headers import Headers
from litestar.enums import ScopeType
from litestar.exceptions.http_exceptions import NotAuthorizedException
from litestar.middleware.authentication import (
    AbstractAuthenticationMiddleware,
    AuthenticationResult,
)
from litestar.types.asgi_types import ASGIApp, HeaderScope

from anibridge.app.logging import get_logger
from anibridge.app.utils.htpasswd import HtpasswdFile

__all__ = ["BasicAuthMiddleware"]

log = get_logger(__name__)


class BasicAuthMiddleware(AbstractAuthenticationMiddleware):
    """Enforce Basic Authentication for HTTP and WebSocket connections."""

    EXEMPT_PATHS: ClassVar[tuple[str, ...]] = (
        r"^/healthz/?$",
        r"^/livez/?$",
        r"^/readyz/?$",
    )

    def __init__(
        self,
        app: ASGIApp,
        username: str | None = None,
        password: str | None = None,
        htpasswd_path: Path | None = None,
        realm: str = "AniBridge",
    ) -> None:
        """Initialize the BasicAuthMiddleware."""
        super().__init__(
            app=app,
            exclude=list(self.EXEMPT_PATHS),
            scopes={ScopeType.HTTP, ScopeType.WEBSOCKET},
        )
        self.username = username
        self.password = password
        self.htpasswd_path = htpasswd_path
        self.realm = realm
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

    def _extract_credentials(self, scope: HeaderScope) -> tuple[str, str] | None:
        """Extract Basic Auth credentials from the request headers."""
        auth_header = Headers(scope["headers"]).get("authorization")
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

    async def authenticate_request(
        self, connection: ASGIConnection
    ) -> AuthenticationResult:
        """Authenticate a connection using Basic authentication credentials."""
        credentials = self._extract_credentials(connection.scope)
        if credentials:
            username, password = credentials
            if self._validate_plain(username, password) or self._validate_htpasswd(
                username, password
            ):
                return AuthenticationResult(user=username, auth="basic")

        log.debug("Authentication failed, sending challenge response")
        raise NotAuthorizedException(
            detail="Authentication required",
            headers={"WWW-Authenticate": f'Basic realm="{self.realm}"'},
        )
