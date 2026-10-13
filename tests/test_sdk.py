import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm

from closedmcp import CLOSEDMCP, Verifier, closedmcp_app, current_user

EMAIL_ONE = "@".join(("user1", "example.com"))  # noqa: FLY002
EMAIL_TWO = "@".join(("user2", "example.com"))  # noqa: FLY002
ISSUER = "https://issuer.example"
RESOURCE = "http://127.0.0.1:8798/mcp"


@pytest.fixture(scope="module")
def keys():
    private = Ed25519PrivateKey.generate()
    jwk = json.loads(OKPAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="test-key", alg="EdDSA", use="sig")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"keys": [jwk]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield private, f"http://127.0.0.1:{server.server_port}/.well-known/jwks.json"
    server.shutdown()
    server.server_close()
    thread.join()


def signed(keys, changes=None, private=None):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": RESOURCE, "sub": "person-123", "email": EMAIL_ONE,
                  "domain": "example.com", "org": "acme", "client": "Test app", "iat": now,
                  "exp": now + 300, "jti": "pass-123"}
    claims.update(changes or {})
    return jwt.encode(claims, private or keys[0], algorithm="EdDSA", headers={"kid": "test-key"})


def verifier(keys, **changes):
    return Verifier(resource=RESOURCE, allowed_domains=["example.com"], issuer=ISSUER, jwks_url=keys[1], **changes)


def test_verifier_happy_path_and_claims(keys):
    assert vars(verifier(keys).verify(f"Bearer {signed(keys)}")) == {
        "id": "person-123", "email": EMAIL_ONE, "company": "example.com", "org": "acme", "aiApp": "Test app"}
    with pytest.raises(RuntimeError):
        current_user()


def test_default_and_explicit_issuers(keys):
    check = Verifier(resource=RESOURCE, allowed_domains=["example.com"], jwks_url=keys[1])
    assert check.issuer == CLOSEDMCP
    assert check.verify(f"Bearer {signed(keys, {'iss': CLOSEDMCP})}").id == "person-123"
    legacy = "https://closedmcp.canpai.io"
    assert check.verify(f"Bearer {signed(keys, {'iss': legacy})}").id == "person-123"
    with pytest.raises(jwt.InvalidIssuerError):
        check.verify(f"Bearer {signed(keys)}")
    assert Verifier(resource=RESOURCE, allowed_domains=["example.com"]).keys[CLOSEDMCP].uri == f"{CLOSEDMCP}/.well-known/jwks.json"
    assert Verifier(resource=RESOURCE, allowed_domains=["example.com"]).keys[legacy].uri == f"{legacy}/.well-known/jwks.json"
    explicit = verifier(keys)
    with pytest.raises(jwt.InvalidIssuerError):
        explicit.verify(f"Bearer {signed(keys, {'iss': CLOSEDMCP})}")


def test_unknown_kid_refresh_cooldown(keys, monkeypatch):
    check = verifier(keys)
    client = check.keys[ISSUER]
    refreshes = []
    original = client.get_signing_keys

    def signing_keys(refresh=False):
        if refresh:
            refreshes.append(True)
        return original(refresh=refresh)

    monkeypatch.setattr(client, "get_signing_keys", signing_keys)
    clock = [100.0]
    monkeypatch.setattr("closedmcp.time.monotonic", lambda: clock[0])
    token = jwt.encode({"iss": ISSUER}, keys[0], algorithm="EdDSA", headers={"kid": "unknown"})
    for _ in range(2):
        with pytest.raises(jwt.PyJWKClientError):
            client.get_signing_key_from_jwt(token)
    assert len(refreshes) == 1
    clock[0] += 31
    with pytest.raises(jwt.PyJWKClientError):
        client.get_signing_key_from_jwt(token)
    assert len(refreshes) == 2
    assert client.timeout == 5


def test_verifier_rejects_invalid_passes(keys):
    now = int(time.time())
    other_key = Ed25519PrivateKey.generate()
    bad = [
        None,
        signed(keys, {"aud": "https://other.example/mcp"}),
        signed(keys, {"aud": [RESOURCE]}),
        signed(keys, {"exp": now - 1, "iat": now - 300}),
        signed(keys, {"exp": now + 301}),
        signed(keys, {"domain": "other.example"}),
        signed(keys, private=other_key),
        signed(keys, {"jti": ""}),
        signed(keys, {"client": 42}),
    ]
    check = verifier(keys)
    for token in bad:
        with pytest.raises((jwt.PyJWTError, ValueError)):
            check.verify(f"Bearer {token}" if token else None)
    with pytest.raises(ValueError, match="allowed_domains"):
        Verifier(resource=RESOURCE, allowed_domains=[])


@asynccontextmanager
async def served_asgi(app):
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8798") as client,
    ):
        yield client


def post(method, params, token, request_id=1):
    return {"method": "POST", "url": "/mcp", "headers": {"authorization": f"Bearer {token}",
                "content-type": "application/json", "accept": "application/json, text/event-stream"},
                "json": {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}}


def result(response):
    assert response.status_code == 200, response.text
    body = response.text
    if body.startswith("event:"):
        body = next(line[6:] for line in body.splitlines() if line.startswith("data: "))
    return json.loads(body)


@pytest.mark.parametrize("library", ["official", "standalone"])
def test_http_metadata_auth_and_concurrent_people(keys, library):
    if library == "official":
        from mcp.server.fastmcp import FastMCP
        app = FastMCP("hello")
    else:
        from fastmcp import FastMCP
        app = FastMCP("hello")

    arrived = 0
    both_arrived = asyncio.Event()

    @app.tool()
    async def hello() -> str:
        nonlocal arrived
        before = current_user().email
        arrived += 1
        if arrived == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=2)
        return f"{before} / {current_user().email}"

    wrapped = closedmcp_app(app, resource=RESOURCE, allowed_domains=["example.com"], issuer=ISSUER, jwks_url=keys[1])

    async def exercise():
        async with served_asgi(wrapped) as client:
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
            assert metadata.json() == {"resource": RESOURCE, "authorization_servers": [ISSUER], "bearer_methods_supported": ["header"]}
            missing = await client.post("/mcp")
            assert missing.status_code == 401
            assert missing.headers["www-authenticate"] == 'Bearer resource_metadata="http://127.0.0.1:8798/.well-known/oauth-protected-resource/mcp"'
            token = signed(keys)
            init = result(await client.request(**post("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}, token)))
            assert init["result"]["serverInfo"]["name"] == "hello"
            listed = result(await client.request(**post("tools/list", {}, token, 2)))
            assert listed["result"]["tools"][0]["name"] == "hello"
            ada = signed(keys)
            bob = signed(keys, {"sub": "person-456", "email": EMAIL_TWO, "jti": "pass-456"})
            calls = await asyncio.gather(
                client.request(**post("tools/call", {"name": "hello", "arguments": {}}, ada, 3)),
                client.request(**post("tools/call", {"name": "hello", "arguments": {}}, bob, 4)),
            )
            assert [result(call)["result"]["content"][0]["text"] for call in calls] == [
                f"{EMAIL_ONE} / {EMAIL_ONE}", f"{EMAIL_TWO} / {EMAIL_TWO}"]
    asyncio.run(exercise())


def test_template_real_mcp_call(keys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    template = Path(__file__).parents[1] / "template" / "server.py"
    env = os.environ.copy()
    env.update(MCP_URL=RESOURCE, ALLOWED_DOMAINS="example.com", PORT=str(port),
               CLOSEDMCP_ORIGIN=ISSUER, CLOSEDMCP_JWKS_URL=keys[1])
    process = subprocess.Popen([sys.executable, str(template)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        async def exercise():
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                for _ in range(100):
                    try:
                        response = await client.get("/.well-known/oauth-protected-resource/mcp")
                        if response.status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    if process.poll() is not None:
                        break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("template did not start")
                assert response.status_code == 200
                token = signed(keys)
                answer = result(await client.request(**post("tools/call", {"name": "hello", "arguments": {}}, token)))
                assert answer["result"]["content"][0]["text"] == f"Hello {EMAIL_ONE}"
        asyncio.run(exercise())
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def test_official_low_level_server(keys):
    from mcp.server.lowlevel import Server
    from mcp.types import TextContent, Tool

    app = Server("lowlevel")

    @app.list_tools()
    async def list_tools():
        return [Tool(name="hello", description="Greet the caller", inputSchema={"type": "object", "properties": {}})]

    @app.call_tool()
    async def call_tool(name, arguments):
        return [TextContent(type="text", text=f"Hello {current_user().email}")]

    wrapped = closedmcp_app(app, resource=RESOURCE, allowed_domains=["example.com"], issuer=ISSUER, jwks_url=keys[1])

    async def exercise():
        async with served_asgi(wrapped) as client:
            answer = result(await client.request(**post("tools/call", {"name": "hello", "arguments": {}}, signed(keys))))
            assert answer["result"]["content"][0]["text"] == f"Hello {EMAIL_ONE}"

    asyncio.run(exercise())
