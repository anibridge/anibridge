"""Basic authentication middleware."""

import base64
import binascii
import re
import secrets
from pathlib import Path
from typing import ClassVar

import bcrypt
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

__all__ = ["BasicAuthMiddleware"]

log = get_logger(__name__)


class BasicAuthMiddleware(AbstractAuthenticationMiddleware):
    """Litestar authentication middleware that enforces HTTP Basic Authentication."""

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
        htpasswd_path: str | Path | None = None,
        path_prefix: str = "",
        realm: str = "AniBridge",
    ) -> None:
        """Initialize the BasicAuthMiddleware."""
        exclude = list(self.EXEMPT_PATHS)
        if path_prefix:
            prefix = re.escape(path_prefix.rstrip("/"))
            exclude.extend(
                pattern.replace("^", f"^{prefix}") for pattern in self.EXEMPT_PATHS
            )

        super().__init__(
            app=app,
            exclude=exclude,
            scopes={ScopeType.HTTP, ScopeType.WEBSOCKET},
        )
        self.username = username
        self.password = password
        self.htpasswd_path = Path(htpasswd_path) if htpasswd_path else None
        self.realm = realm

    def _validate_plain(self, username: str, password: str) -> bool:
        """Validate plain username and password credentials."""
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

    def _validate_htpasswd(self, username: str, password: str) -> bool:
        """Validate bcrypt htpasswd credentials."""
        if self.htpasswd_path is None:
            return False

        try:
            lines = self.htpasswd_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            log.warning("Unable to read htpasswd file: %s", self.htpasswd_path)
            return False

        for line in lines:
            entry_username, separator, hashed_password = line.partition(":")
            if separator and secrets.compare_digest(entry_username, username):
                try:
                    return bcrypt.checkpw(
                        password.encode(), hashed_password.strip().encode()
                    )
                except ValueError:
                    return False
        return False

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
        """Authenticate an HTTP request using Basic authentication credentials."""
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
