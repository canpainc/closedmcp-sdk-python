import asyncio
import json
from contextlib import AsyncExitStack

import httpx
import pytest
from starlette.datastructures import Headers

from closedmcp import hosted
from closedmcp.hosted import (
    CHANNEL_KEY,
    EVENTS_KEY,
    HEADERS,
    Caller,
    caller,
    database_url,
    events,
    live_database_url,
    open_channel,
    try_caller,
)

EMAIL = "@".join(("bob", "acme.dev"))  # noqa: FLY002
FAKE_HEADERS = {
    "X-ClosedMCP-User": "u-123",
    "X-ClosedMCP-Email": EMAIL,
    "X-ClosedMCP-Org": "acme",
    "X-ClosedMCP-Client": "Claude",
}
EXPECTED = Caller(id="u-123", email=EMAIL, org="acme", client="Claude")


def test_caller_from_mappings_and_requests():
    assert caller(FAKE_HEADERS) == EXPECTED
    assert caller({k.lower(): v for k, v in FAKE_HEADERS.items()}) == EXPECTED
    assert caller(Headers(FAKE_HEADERS)) == EXPECTED

    class Request:
        headers = Headers(FAKE_HEADERS)

    class Context:
        class request_context:
            request = Request()

    assert caller(Request()) == EXPECTED
    assert caller(Context()) == EXPECTED


def test_caller_missing_headers():
    for missing in HEADERS.values():
        headers = {k.lower(): v for k, v in FAKE_HEADERS.items()}
        headers[missing] = ""
        assert try_caller(headers) is None
        with pytest.raises(RuntimeError, match="X-ClosedMCP"):
            caller(headers)
    # Outside any request.
    assert try_caller() is None
    with pytest.raises(RuntimeError, match="running on ClosedMCP"):
        caller()
    assert try_caller(object()) is None


def test_open_channel():
    assert open_channel("room:general") == {CHANNEL_KEY: {"topic": "room:general"}}
    assert open_channel("t", name="#general", ttl=600) == {
        "closedmcp/channel": {"topic": "t", "name": "#general", "ttl": 600}}
    assert open_channel("é" * hosted.TOPIC_MAX_CHARS)
    assert open_channel("t", ttl=30) and open_channel("t", ttl=604_800)
    for args, kwargs in [
        (("",), {}), ((7,), {}), (("x" * 201,), {}),
        (("t",), {"name": "n" * 81}), (("t",), {"name": 1}),
        (("t",), {"ttl": 29}), (("t",), {"ttl": 604_801}), (("t",), {"ttl": 60.0}),
        (("t",), {"ttl": "60"}), (("t",), {"ttl": True}),
    ]:
        with pytest.raises(ValueError, match="open_channel"):
            open_channel(*args, **kwargs)


def test_events():
    data = {"from": EMAIL, "text": "hi"}
    assert events([{"topic": "room:general", "data": data}]) == {
        EVENTS_KEY: [{"topic": "room:general", "data": data}]}
    assert events([]) == {"closedmcp/events": []}
    assert len(events([{"topic": "t", "data": 1}] * 20)[EVENTS_KEY]) == 20
    assert events([{"topic": "t", "data": None}])[EVENTS_KEY] == [{"topic": "t", "data": None}]
    # Exactly 8 KB of compact JSON passes; one more byte (or a multi-byte char) does not.
    assert events([{"topic": "t", "data": "x" * (8192 - 2)}])
    for bad, match in [
        ([{"topic": "t", "data": "x" * (8192 - 1)}], "8192 bytes"),
        ([{"topic": "t", "data": "é" * 4096}], "8192 bytes"),
        ([{"topic": "t", "data": 1}] * 21, "at most 20"),
        ([{"topic": "t", "data": {1, 2}}], "JSON value"),
        ([{"topic": "t", "data": float("nan")}], "JSON value"),
        ([{"topic": "t"}], "events\\[0\\]"),
        ([{"topic": "", "data": 1}], "events\\[0\\]: topic"),
        ({"topic": "t", "data": 1}, "list"),
        ("tt", "list"),
    ]:
        with pytest.raises(ValueError, match=match):
            events(bad)


def test_database_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LIVE_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="closedmcp_database_enable"):
        database_url()
    assert live_database_url() is None
    monkeypatch.setenv("DATABASE_URL", "postgresql://a@h/db")
    monkeypatch.setenv("LIVE_DATABASE_URL", "postgresql://r@h/live")
    assert database_url() == "postgresql://a@h/db"
    assert live_database_url() == "postgresql://r@h/live"


def _post(method, params, headers, request_id=1):
    return {"method": "POST", "url": "/mcp", "headers": {**headers, "content-type": "application/json",
            "accept": "application/json, text/event-stream"},
            "json": {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}}


def _result(response):
    assert response.status_code == 200, response.text
    body = response.text
    if body.startswith("event:"):
        body = next(line[6:] for line in body.splitlines() if line.startswith("data: "))
    return json.loads(body)["result"]


async def _calls(asgi, lifespan, *calls):
    """Run each (name, arguments, headers) tools/call against one served app."""
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(lifespan)
        client = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url="http://127.0.0.1:8080"))
        return [_result(await client.request(**_post("tools/call", {"name": n, "arguments": a}, h)))
                for n, a, h in calls]


def test_official_fastmcp_tool():
    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolResult, TextContent

    app = FastMCP("chat", stateless_http=True, json_response=True)

    @app.tool()
    def post(room: str, text: str) -> CallToolResult:
        me = caller()
        return CallToolResult(
            content=[TextContent(type="text", text=f"Sent as {me.email}.")],
            _meta={**open_channel(f"room:{room}"), **events([{"topic": f"room:{room}", "data": {"from": me.id, "text": text}}])},
        )

    asgi = app.streamable_http_app()
    answer, anonymous = asyncio.run(_calls(
        asgi, app.session_manager.run(),
        ("post", {"room": "general", "text": "hi"}, FAKE_HEADERS),
        ("post", {"room": "g", "text": "hi"}, {}),
    ))
    assert answer["content"][0]["text"] == f"Sent as {EMAIL}."
    assert answer["_meta"] == {
        "closedmcp/channel": {"topic": "room:general"},
        "closedmcp/events": [{"topic": "room:general", "data": {"from": "u-123", "text": "hi"}}],
    }
    assert anonymous["isError"] is True
    assert "X-ClosedMCP" in anonymous["content"][0]["text"]


def test_fastmcp2_tool_with_context_and_without():
    from fastmcp import Context, FastMCP
    from fastmcp.tools.tool import ToolResult

    app = FastMCP("chat")

    @app.tool
    def whoami(ctx: Context) -> ToolResult:
        me = caller(ctx)
        assert caller() == me
        return ToolResult(content=me.email, meta=events([{"topic": "seen", "data": me.__dict__}]))

    asgi = app.http_app(stateless_http=True, json_response=True)
    [answer] = asyncio.run(_calls(asgi, asgi.router.lifespan_context(asgi), ("whoami", {}, FAKE_HEADERS)))
    assert answer["content"][0]["text"] == EMAIL
    assert answer["_meta"] == {"closedmcp/events": [{"topic": "seen", "data": EXPECTED.__dict__}]}
