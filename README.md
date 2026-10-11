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

`resource` is the exact public MCP URL and JWT audience. `allowed_domains` must contain at least one verified company domain. The wrapper checks an EdDSA pass against the issuer's JWKS, issuer, exact audience, expiry, five-minute maximum lifetime, required identity claims, and company domain. Unknown key IDs trigger a JWKS refresh through PyJWT. `current_user()` returns a `Person` with `id`, `email`, `company`, `org`, and optional `aiApp`; it raises outside a verified request. `aiApp` is a client supplied display value.

For `/mcp`, metadata is at `/.well-known/oauth-protected-resource/mcp` and the generic metadata path. A missing or invalid pass receives `401` with `WWW-Authenticate: Bearer resource_metadata="<metadata URL>"`. Authenticated non-POST requests receive `405`. To use another ASGI server, call `closedmcp_app(app, resource=..., allowed_domains=...)` and serve the returned ASGI app. Its lifespan must be run by the ASGI server.

See the [ClosedMCP documentation](https://closedmcp.canpai.io/docs).
