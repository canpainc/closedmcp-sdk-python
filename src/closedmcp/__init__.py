"""ClosedMCP sign-in for MCP Streamable HTTP servers."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import jwt
from jwt import PyJWKClient, PyJWKClientError
from mcp.server.auth.middleware.auth_context import (
    AuthContextMiddleware,
    get_access_token,
)
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP as OfficialFastMCP
from mcp.server.lowlevel import Server as OfficialServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route

CLOSEDMCP = "https://closedmcp.com"
REQUIRED_SCOPE = "mcp:tools"
CLAIMS = "https://closedmcp.com/claims/"
_BEARER = re.compile(r"Bearer\s+(\S+)\Z", re.IGNORECASE)


class _JWKClient(PyJWKClient):
    def __init__(self, uri: str) -> None:
        super().__init__(uri, timeout=5)
        self._refresh_lock = threading.Lock()
        self._last_refresh = float("-inf")

    def get_signing_key(self, kid: str):
        with self._refresh_lock:
            signing_key = self.match_kid(self.get_signing_keys(), kid)
            if signing_key is None and time.monotonic() - self._last_refresh >= 30:
                self._last_refresh = time.monotonic()
                signing_key = self.match_kid(self.get_signing_keys(refresh=True), kid)
            if signing_key is None:
                raise PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
            return signing_key


@dataclass(frozen=True)
class Person:
    id: str
    email: str
    company: str
    org: str
    aiApp: str | None = None


def current_user() -> Person:
    """The verified person for this MCP request."""
    token = get_access_token()
    if not isinstance(token, _AccessToken):
        raise RuntimeError("No verified ClosedMCP person in this request")  # noqa: TRY004 - preserve the helper API
    return token.person


class _AccessToken(AccessToken):
    person: Person


class Verifier:
    """Check an OAuth access token and its customer company against a required allowlist."""

    def __init__(
        self,
        *,
        resource: str,
        allowed_domains: Iterable[str],
        issuer: str | None = None,
        jwks_url: str | None = None,
    ) -> None:
        url = urlsplit(resource)
        if url.scheme not in ("http", "https") or not url.netloc or url.query or url.fragment:
            raise ValueError("resource must be an absolute HTTP(S) MCP URL without query or fragment")
        self.resource = resource
        self.issuer = issuer or CLOSEDMCP
        self.allowed_domains = {domain.strip().lower() for domain in allowed_domains if domain.strip()}
        if not self.allowed_domains:
            raise ValueError("allowed_domains must name at least one company")
        self.keys = _JWKClient(jwks_url or f"{self.issuer}/.well-known/jwks.json")

    def verify(self, authorization: str | None) -> Person:
        match = _BEARER.fullmatch(authorization or "")
        if match is None:
            raise jwt.InvalidTokenError("No sign-in token")
        token = match.group(1)
        return self._access_token(token, require_scope=True).person

    async def verify_token(self, token: str) -> AccessToken | None:
        """Official MCP TokenVerifier hook used by its bearer middleware."""
        try:
            return await asyncio.to_thread(self._access_token, token)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OSError):
            return None

    def _access_token(self, token: str, *, require_scope: bool = False) -> _AccessToken:
        if jwt.get_unverified_header(token).get("typ") not in ("at+jwt", "application/at+jwt"):
            raise jwt.InvalidTokenError("Not a JWT access token")
        claims = jwt.decode(
            token, self.keys.get_signing_key_from_jwt(token).key, algorithms=["EdDSA"],
            issuer=self.issuer, audience=self.resource,
            options={"require": ["iss", "aud", "sub", "email", f"{CLAIMS}domain", f"{CLAIMS}org", "iat", "exp", "jti", "client_id", "scope"], "strict_aud": True},
        )
        if claims["aud"] != self.resource:
            raise jwt.InvalidTokenError("Wrong MCP audience")
        issued, expires = claims["iat"], claims["exp"]
        if (type(issued) not in (int, float) or type(expires) not in (int, float)
                or expires <= issued or expires - issued > 300):
            raise jwt.InvalidTokenError("Sign-in token lifetime is invalid")
        for name in ("sub", "email", f"{CLAIMS}domain", f"{CLAIMS}org", "jti", "client_id"):
            if not isinstance(claims[name], str) or not claims[name]:
                raise jwt.InvalidTokenError("Sign-in token claims are invalid")
        if f"{CLAIMS}client" in claims and not isinstance(claims[f"{CLAIMS}client"], str):
            raise jwt.InvalidTokenError("Sign-in token claims are invalid")
        if claims[f"{CLAIMS}domain"].lower() not in self.allowed_domains:
            raise jwt.InvalidTokenError("Company is not allowed")
        if not isinstance(claims["scope"], str):
            raise jwt.InvalidTokenError("Invalid scope claim")
        scopes = claims["scope"].split()
        if require_scope and REQUIRED_SCOPE not in scopes:
            raise jwt.InvalidTokenError("Insufficient scope")
        person = Person(claims["sub"], claims["email"], claims[f"{CLAIMS}domain"], claims[f"{CLAIMS}org"], claims.get(f"{CLAIMS}client"))
        return _AccessToken(token=token, client_id=claims["client_id"], scopes=scopes,
                            expires_at=claims["exp"], resource=self.resource, subject=claims["sub"],
                            claims=claims, person=person)


class _ProtectedApp:
    def __init__(self, app: Any, verifier: Verifier) -> None:
        self.app = app
        self.verifier = verifier
        url = urlsplit(verifier.resource)
        self.path = url.path or "/"
        self.metadata_path = "/.well-known/oauth-protected-resource" + ("" if self.path == "/" else self.path)
        self.metadata_url = f"{url.scheme}://{url.netloc}{self.metadata_path}"
        self.protected = AuthenticationMiddleware(
            AuthContextMiddleware(RequireAuthMiddleware(self._serve, [REQUIRED_SCOPE], AnyHttpUrl(self.metadata_url))),
            backend=BearerAuthBackend(verifier),
        )
        self.metadata = {
            "resource": verifier.resource,
            "authorization_servers": [verifier.issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [REQUIRED_SCOPE],
        }

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if scope["method"] == "GET" and path in (self.metadata_path, "/.well-known/oauth-protected-resource"):
            await self._reply(send, 200, self.metadata)
            return
        if path != self.path:
            await self._reply(send, 404, {"error": "not found"})
            return
        await self.protected(scope, receive, send)

    async def _serve(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["method"] != "POST":
            await self._reply(send, 405, {"error": "method not allowed"})
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reply(send: Any, status: int, body: dict, headers: list[tuple[bytes, bytes]] | None = None) -> None:
        data = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), *(headers or [])]})
        await send({"type": "http.response.body", "body": data})


class _MCPHandler:
    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        await self.manager.handle_request(scope, receive, send)


def closedmcp_app(
    app: Any,
    *,
    resource: str,
    allowed_domains: Iterable[str],
    issuer: str | None = None,
    jwks_url: str | None = None,
) -> Any:
    """Return a protected ASGI app for an official MCP or standalone FastMCP server."""
    verifier = Verifier(resource=resource, allowed_domains=allowed_domains, issuer=issuer, jwks_url=jwks_url)
    path = urlsplit(resource).path or "/"
    if isinstance(app, OfficialFastMCP):
        app.settings.streamable_http_path = path
        app.settings.stateless_http = True
        http_app = app.streamable_http_app()
    elif isinstance(app, OfficialServer):
        manager = StreamableHTTPSessionManager(app=app, stateless=True)
        http_app = Starlette(
            routes=[Route(path, endpoint=_MCPHandler(manager))],
            lifespan=lambda _: manager.run(),
        )
    elif type(app).__module__.startswith("fastmcp.") and hasattr(app, "http_app"):
        http_app = app.http_app(path=path, stateless_http=True)
    else:
        raise TypeError("app must be an official MCP Server or a FastMCP server")
    return _ProtectedApp(http_app, verifier)


def serve_with_closedmcp(
    app: Any,
    *,
    resource: str,
    allowed_domains: Iterable[str],
    issuer: str | None = None,
    jwks_url: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> None:
    """Serve a protected MCP over Streamable HTTP. Blocks until stopped."""
    import uvicorn

    uvicorn.run(
        closedmcp_app(app, resource=resource, allowed_domains=allowed_domains, issuer=issuer, jwks_url=jwks_url),
        host=host,
        port=port,
    )


__all__ = ["CLOSEDMCP", "Person", "Verifier", "closedmcp_app", "current_user", "serve_with_closedmcp"]
