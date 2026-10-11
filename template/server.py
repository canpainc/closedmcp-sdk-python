"""Run with MCP_URL and ALLOWED_DOMAINS set; see ../README.md."""

import os

from mcp.server.fastmcp import FastMCP

from closedmcp import current_user, serve_with_closedmcp

resource = os.environ["MCP_URL"]
allowed_domains = [domain.strip() for domain in os.environ["ALLOWED_DOMAINS"].split(",")]

app = FastMCP("hello")


@app.tool()
def hello() -> str:
    return f"Hello {current_user().email}"


if __name__ == "__main__":
    serve_with_closedmcp(
        app,
        resource=resource,
        allowed_domains=allowed_domains,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8787")),
        issuer=os.environ.get("CLOSEDMCP_ORIGIN", "https://closedmcp.canpai.io"),
        jwks_url=os.environ.get("CLOSEDMCP_JWKS_URL"),
    )
