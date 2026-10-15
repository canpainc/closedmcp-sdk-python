"""Helpers for Serverless MCPs hosted on ClosedMCP.

No network and no sign-in: ClosedMCP has already checked the caller and passes
them in ``X-ClosedMCP-*`` headers. Works with FastMCP 2 and the official
``mcp`` SDK (``mcp.server.fastmcp`` or the low-level server).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

HOSTED_PORT = 8080
"""Hosted apps serve MCP on ``0.0.0.0:8080`` at ``/mcp``."""
HOSTED_PATH = "/mcp"

HEADERS = {
    "id": "x-closedmcp-user",
    "email": "x-closedmcp-email",
    "org": "x-closedmcp-org",
    "client": "x-closedmcp-client",
}
"""Request headers the ClosedMCP runner sets on every call (lowercase)."""

TOPIC_MAX_CHARS = 200
NAME_MAX_CHARS = 80
TTL_MIN_SECONDS = 30
TTL_MAX_SECONDS = 7 * 86_400
TTL_DEFAULT_SECONDS = 86_400
EVENTS_PER_RESULT = 20
EVENT_DATA_MAX_BYTES = 8 << 10
EVENTS_PER_CHANNEL_PER_HOUR = 60

CHANNEL_KEY = "closedmcp/channel"
EVENTS_KEY = "closedmcp/events"


@dataclass(frozen=True)
class Caller:
    """Who is calling. All four are always set by ClosedMCP."""

    id: str
    """Person ID, stable."""
    email: str
    org: str
    """Company org ID."""
    client: str
    """AI app, for display only."""


def _current_headers() -> Mapping[str, str] | None:
    try:
        from mcp.server.lowlevel.server import request_ctx

        request = request_ctx.get().request
        if request is not None:
            return request.headers
    except (ImportError, LookupError, AttributeError):
        pass
    try:
        from fastmcp.server.dependencies import get_http_headers

        return get_http_headers(include_all=True) or None
    except (ImportError, RuntimeError):
        return None


def _headers_of(source: Any) -> Mapping[str, str] | None:
    if source is None:
        return _current_headers()
    if isinstance(source, Mapping):
        return source
    request_context = getattr(source, "request_context", None)  # MCP / FastMCP Context
    if request_context is not None:
        source = getattr(request_context, "request", None)
    headers = getattr(source, "headers", None)  # Starlette Request
    return headers if isinstance(headers, Mapping) else None


def try_caller(source: Any = None) -> Caller | None:
    """The caller, or None when the request did not come through ClosedMCP.

    ``source`` may be omitted inside a tool (the current request is used), or be
    a tool ``Context``, a Starlette ``Request``, or a headers mapping.
    """
    headers = _headers_of(source)
    if headers is None:
        return None
    lowered = {str(k).lower(): v for k, v in headers.items()}
    values = {field: lowered.get(name) for field, name in HEADERS.items()}
    if not all(isinstance(v, str) and v for v in values.values()):
        return None
    return Caller(**values)


def caller(source: Any = None) -> Caller:
    """The caller; raises when the X-ClosedMCP-* headers are missing."""
    found = try_caller(source)
    if found is None:
        raise RuntimeError(
            "No ClosedMCP caller: X-ClosedMCP-User/Email/Org/Client headers are missing "
            "(is this running on ClosedMCP?)"
        )
    return found


def _check_topic(topic: Any, where: str) -> str:
    if not isinstance(topic, str) or not topic:
        raise ValueError(f"{where}: topic must be a non-empty string")
    if len(topic) > TOPIC_MAX_CHARS:
        raise ValueError(f"{where}: topic is over {TOPIC_MAX_CHARS} characters")
    return topic


def open_channel(topic: str, *, name: str | None = None, ttl: int | None = None) -> dict[str, Any]:
    """``_meta`` that opens an event channel the caller can wait on.

    ``ttl`` is whole seconds from 30 to 7 days; ClosedMCP defaults to 1 day.
    """
    channel: dict[str, Any] = {"topic": _check_topic(topic, "open_channel")}
    if name is not None:
        if not isinstance(name, str):
            raise ValueError("open_channel: name must be a string")
        if len(name) > NAME_MAX_CHARS:
            raise ValueError(f"open_channel: name is over {NAME_MAX_CHARS} characters")
        channel["name"] = name
    if ttl is not None:
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not TTL_MIN_SECONDS <= ttl <= TTL_MAX_SECONDS:
            raise ValueError(f"open_channel: ttl must be whole seconds from {TTL_MIN_SECONDS} to {TTL_MAX_SECONDS}")
        channel["ttl"] = ttl
    return {CHANNEL_KEY: channel}


def events(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``_meta`` that delivers each ``{"topic", "data"}`` to the app's open channels with that topic."""
    if isinstance(items, (str, bytes, Mapping)) or not isinstance(items, Sequence):
        raise ValueError("events: give a list of {'topic', 'data'}")  # noqa: TRY004 one error type for all checks
    if len(items) > EVENTS_PER_RESULT:
        raise ValueError(f"events: at most {EVENTS_PER_RESULT} per result")
    out = []
    for i, event in enumerate(items):
        if not isinstance(event, Mapping) or "data" not in event:
            raise ValueError(f"events[{i}]: give {{'topic', 'data'}}")
        topic = _check_topic(event.get("topic"), f"events[{i}]")
        try:
            data = json.dumps(event["data"], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as e:
            raise ValueError(f"events[{i}]: data must be a JSON value ({e})") from None
        if len(data.encode()) > EVENT_DATA_MAX_BYTES:
            raise ValueError(f"events[{i}]: data is over {EVENT_DATA_MAX_BYTES} bytes as JSON")
        out.append({"topic": topic, "data": json.loads(data)})
    return {EVENTS_KEY: out}


def database_url() -> str:
    """The app's PostgreSQL URL (live when published, dev in runs). Raises if no database is enabled."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set: enable a database with closedmcp_database_enable")
    return url


def live_database_url() -> str | None:
    """Read-only URL for the live database; set only in ``closedmcp_run`` sandboxes."""
    return os.environ.get("LIVE_DATABASE_URL") or None


__all__ = [
    "CHANNEL_KEY",
    "EVENTS_KEY",
    "EVENTS_PER_CHANNEL_PER_HOUR",
    "EVENTS_PER_RESULT",
    "EVENT_DATA_MAX_BYTES",
    "HEADERS",
    "HOSTED_PATH",
    "HOSTED_PORT",
    "NAME_MAX_CHARS",
    "TOPIC_MAX_CHARS",
    "TTL_DEFAULT_SECONDS",
    "TTL_MAX_SECONDS",
    "TTL_MIN_SECONDS",
    "Caller",
    "caller",
    "database_url",
    "events",
    "live_database_url",
    "open_channel",
    "try_caller",
]
