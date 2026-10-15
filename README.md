# closedmcp

A small Python wrapper for Log in with ClosedMCP. It protects Streamable HTTP requests made to the official SDK’s `Server` or built-in `FastMCP`, or standalone FastMCP 2, verifies a signed pass on every call, and serves MCP protected-resource metadata.

## Add to an existing server

Keep your existing official `mcp.server.fastmcp.FastMCP` or standalone `fastmcp.FastMCP` 2 instance. Replace its HTTP serving call with:

```python
import os
from closedmcp import current_user, serve_with_closedmcp
serve_with_closedmcp(app, resource=os.environ["MCP_URL"], allowed_domains=os.environ["ALLOWED_DOMAINS"].split(","))
```

In any tool, use `current_user().email` or `current_user().org`. For an existing ASGI host, serve `closedmcp_app(app, resource=..., allowed_domains=...)` instead. Install with `pip install closedmcp` for the official SDK or `pip install 'closedmcp[fastmcp]'` for standalone FastMCP 2.

## Hello

```python
from mcp.server.fastmcp import FastMCP
from closedmcp import current_user, serve_with_closedmcp
app = FastMCP("hello")
@app.tool()
def hello() -> str:
    return f"Hello {current_user().email}"
serve_with_closedmcp(
    app,
    resource="https://example.com/mcp",
    allowed_domains=["example.com"])
```

Replace the first import with `from fastmcp import FastMCP` to use standalone FastMCP 2. Install with `pip install closedmcp` for the official SDK or `pip install 'closedmcp[fastmcp]'` for both. This repository also has a [runnable template](template/server.py): install the package locally with `pip install -e .`, then run `MCP_URL=https://example.com/mcp ALLOWED_DOMAINS=example.com python template/server.py`. Set `PORT` and `HOST` for the listening socket. `CLOSEDMCP_ORIGIN` and `CLOSEDMCP_JWKS_URL` are available for local integration tests.

`resource` is the exact public MCP URL and JWT audience. `allowed_domains` must contain at least one verified company domain. By default, the wrapper accepts passes from `https://closedmcp.com` and the legacy `https://closedmcp.canpai.io`, using each issuer's JWKS. An explicit `issuer` accepts only that issuer. The wrapper checks the EdDSA signature, issuer, exact audience, expiry, five-minute maximum lifetime, required identity claims, and company domain. Unknown key IDs trigger at most one JWKS refresh per 30 seconds. `current_user()` returns a `Person` with `id`, `email`, `company`, `org`, and optional `aiApp`; it raises outside a verified request. `aiApp` is a client supplied display value.

For `/mcp`, metadata is at `/.well-known/oauth-protected-resource/mcp` and the generic metadata path. A missing or invalid pass receives `401` with `WWW-Authenticate: Bearer resource_metadata="<metadata URL>"`. Authenticated non-POST requests receive `405`. To use another ASGI server, call `closedmcp_app(app, resource=..., allowed_domains=...)` and serve the returned ASGI app. Its lifespan must be run by the ASGI server.

## Hosted on ClosedMCP (Serverless MCPs)

An app hosted on ClosedMCP does not use sign-in: ClosedMCP checks the caller and strips `Authorization` (so `serve_with_closedmcp` would reject every call). Use `closedmcp.hosted` instead. Serve `/mcp` on `0.0.0.0:8080` (`HOSTED_PATH`, `HOSTED_PORT`).

```python
from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from closedmcp.hosted import HOSTED_PORT, caller, events, open_channel

mcp = FastMCP("chat")

@mcp.tool
def watch(room: str) -> ToolResult:
    return ToolResult(content=f"Watching #{room}.", meta=open_channel(f"room:{room}", name=f"#{room}"))

@mcp.tool
def post(room: str, text: str) -> ToolResult:
    return ToolResult(content="Sent.", meta=events([{"topic": f"room:{room}", "data": {"from": caller().email, "text": text}}]))

mcp.run(transport="http", host="0.0.0.0", port=HOSTED_PORT, path="/mcp", stateless_http=True)
```

- `caller()` returns `Caller(id, email, org, client)` from the `X-ClosedMCP-*` headers of the current request and raises when they are missing. It works in FastMCP 2 and official `mcp` SDK tools; you may also pass a tool `Context`, a Starlette `Request`, or a headers mapping. `try_caller()` returns `None` instead of raising.
- `open_channel(topic, name=None, ttl=None)` and `events([{"topic", "data"}])` build the `_meta` keys and raise `ValueError` on anything ClosedMCP would drop or change (topic at most 200 characters, name 80, `ttl` whole seconds from 30 to 7 days, at most 20 events, `data` at most 8 KB as JSON). Combine them with `{**open_channel(...), **events(...)}`. With the official SDK, return `CallToolResult(content=..., _meta=...)`.
- `database_url()` returns `DATABASE_URL` (raises if no database is enabled); `live_database_url()` returns the read-only live URL in `closedmcp_run` sandboxes.

See the [ClosedMCP documentation](https://closedmcp.com/docs).
