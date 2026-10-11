"""ClosedMCP sign-in for MCP Streamable HTTP servers."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import jwt
from jwt import PyJWKClient
from mcp.server.fastmcp import FastMCP as OfficialFastMCP
from mcp.server.lowlevel import Server as OfficialServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route

CLOSEDMCP = "https://closedmcp.canpai.io"
_person: ContextVar[Person | None] = ContextVar("closedmcp_person", default=None)
_BEARER = re.compile(r"Bearer\s+(\S+)\Z", re.IGNORECASE)


@dataclass(frozen=True)
class Person:
    id: str
    email: str
    company: str
    org: str
    aiApp: str | None = None


def current_user() -> Person:
    """The verified person for this MCP request."""
    person = _person.get()
    if person is None:
        raise RuntimeError("No verified ClosedMCP person in this request")
    return person


class Verifier:
    """Check a signed pass and its customer company against a required allowlist."""

    def __init__(
        self,
        *,
        resource: str,
        allowed_domains: Iterable[str],
        issuer: str = CLOSEDMCP,
        jwks_url: str | None = None,
    ) -> None:
        url = urlsplit(resource)
        if url.scheme not in ("http", "https") or not url.netloc or url.query or url.fragment:
            raise ValueError("resource must be an absolute HTTP(S) MCP URL without query or fragment")
        self.resource = resource
        self.issuer = issuer
        self.allowed_domains = {domain.strip().lower() for domain in allowed_domains if domain.strip()}
        if not self.allowed_domains:
            raise ValueError("allowed_domains must name at least one company")
        self.keys = PyJWKClient(jwks_url or f"{issuer}/.well-known/jwks.json")

    def verify(self, authorization: str | None) -> Person:
        match = _BEARER.fullmatch(authorization or "")
        if match is None:
            raise jwt.InvalidTokenError("No sign-in token")
        token = match.group(1)
        claims = jwt.decode(
            token,
            self.keys.get_signing_key_from_jwt(token).key,
            algorithms=["EdDSA"],
            issuer=self.issuer,
            audience=self.resource,
            options={
                "require": ["iss", "aud", "sub", "email", "domain", "org", "iat", "exp", "jti"],
                "strict_aud": True,
            },
        )
        if claims["aud"] != self.resource:
            raise jwt.InvalidTokenError("Wrong MCP audience")
        issued, expires = claims["iat"], claims["exp"]
        if (type(issued) not in (int, float) or type(expires) not in (int, float)
                or expires <= issued or expires - issued > 300):
            raise jwt.InvalidTokenError("Sign-in token lifetime is invalid")
        for name in ("sub", "email", "domain", "org", "jti"):
            if not isinstance(claims[name], str) or not claims[name]:
                raise jwt.InvalidTokenError("Sign-in token claims are invalid")
        if "client" in claims and not isinstance(claims["client"], str):
            raise jwt.InvalidTokenError("Sign-in token claims are invalid")
        if claims["domain"].lower() not in self.allowed_domains:
            raise jwt.InvalidTokenError("Company is not allowed")
        return Person(claims["sub"], claims["email"], claims["domain"], claims["org"], claims.get("client"))


class _ProtectedApp:
    def __init__(self, app: Any, verifier: Verifier) -> None:
        self.app = app
        self.verifier = verifier
        url = urlsplit(verifier.resource)
        self.path = url.path or "/"
        self.metadata_path = "/.well-known/oauth-protected-resource" + ("" if self.path == "/" else self.path)
        self.metadata_url = f"{url.scheme}://{url.netloc}{self.metadata_path}"
        self.metadata = {
            "resource": verifier.resource,
            "authorization_servers": [verifier.issuer],
            "bearer_methods_supported": ["header"],
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
        authorization = next((value.decode("latin-1") for key, value in scope["headers"] if key.lower() == b"authorization"), None)
        try:
            person = await asyncio.to_thread(self.verifier.verify, authorization)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OSError):
            await self._reply(send, 401, {"error": "unauthorized"}, [(b"www-authenticate", f'Bearer resource_metadata="{self.metadata_url}"'.encode())])
            return
        if scope["method"] != "POST":
            await self._reply(send, 405, {"error": "method not allowed"})
            return
        token = _person.set(person)
        try:
            await self.app(scope, receive, send)
        finally:
            _person.reset(token)

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
    issuer: str = CLOSEDMCP,
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
    issuer: str = CLOSEDMCP,
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
