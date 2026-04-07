"""
Local Kiro bridge for AI Factory sub-agents.
Same Okta 3LO flow as kiro_bridge.py but forwards to a local sub-agent
instead of AgentCore. Used for local testing before deploying to AgentCore.

Usage in mcp.json:
  "command": "python",
  "args": ["sap-smart-agent/agents/local_bridge.py"],
  "env": {
    "LOCAL_AGENT_URL": "http://localhost:8101/mcp",   ← which sub-agent
    "OKTA_REDIRECT_URI": "http://localhost:8090/callback",  ← unique port per bridge
    ...okta env vars...
  }
"""
import os, sys, json, base64, time, asyncio, webbrowser, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# ── Okta config ───────────────────────────────────────────────────────────────
OKTA_DOMAIN       = os.environ.get("OKTA_DOMAIN",       "trial-1053860.okta.com")
OKTA_AUTH_SERVER  = os.environ.get("OKTA_AUTH_SERVER",  "default")
OKTA_CLIENT_ID    = os.environ["OKTA_CLIENT_ID"]
OKTA_CLIENT_SECRET= os.environ["OKTA_CLIENT_SECRET"]
OKTA_SCOPES       = os.environ.get("OKTA_SCOPES",       "openid email")
OKTA_REDIRECT_URI = os.environ.get("OKTA_REDIRECT_URI", "http://localhost:8086/callback")
OKTA_CALLBACK_PORT= int(OKTA_REDIRECT_URI.split(":")[-1].split("/")[0])

OKTA_AUTHORIZE_URL = f"https://{OKTA_DOMAIN}/oauth2/{OKTA_AUTH_SERVER}/v1/authorize"
OKTA_TOKEN_URL     = f"https://{OKTA_DOMAIN}/oauth2/{OKTA_AUTH_SERVER}/v1/token"

# ── Target local sub-agent ────────────────────────────────────────────────────
LOCAL_AGENT_URL = os.environ.get("LOCAL_AGENT_URL", "http://localhost:8101/mcp")

TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           ".local_bridge_token_cache.json")


# ── Okta 3LO ─────────────────────────────────────────────────────────────────
class _CallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    def do_GET(self, *a, **k):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authenticated! Return to Kiro.</h2>")
        else:
            self.send_response(400); self.end_headers()
    def log_message(self, *a): pass


def _load_cached_token() -> str:
    try:
        with open(TOKEN_CACHE) as f:
            token = json.load(f).get("access_token", "")
        if token:
            p = token.split(".")[1]
            p += "=" * (4 - len(p) % 4)
            if json.loads(base64.b64decode(p)).get("exp", 0) > time.time() + 60:
                return token
    except Exception:
        pass
    return ""


def _save_token(token: str):
    with open(TOKEN_CACHE, "w") as f:
        json.dump({"access_token": token}, f)


def get_okta_token() -> str:
    cached = _load_cached_token()
    if cached:
        return cached
    # Open browser for 3LO
    params = {"client_id": OKTA_CLIENT_ID, "response_type": "code",
              "scope": OKTA_SCOPES, "redirect_uri": OKTA_REDIRECT_URI, "state": "abc"}
    url = f"{OKTA_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    srv = HTTPServer(("localhost", OKTA_CALLBACK_PORT), _CallbackHandler)
    _CallbackHandler.auth_code = None
    t = Thread(target=srv.handle_request, daemon=True)
    t.start()
    webbrowser.open(url)
    t.join(timeout=120)
    srv.server_close()
    if not _CallbackHandler.auth_code:
        raise RuntimeError("Okta auth timed out — no code received")
    creds = base64.b64encode(f"{OKTA_CLIENT_ID}:{OKTA_CLIENT_SECRET}".encode()).decode()
    with httpx.Client() as c:
        r = c.post(OKTA_TOKEN_URL,
                   data={"grant_type": "authorization_code",
                         "code": _CallbackHandler.auth_code,
                         "redirect_uri": OKTA_REDIRECT_URI},
                   headers={"Authorization": f"Basic {creds}",
                             "Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        token = r.json()["access_token"]
        _save_token(token)
        return token


# ── Stdio bridge → local sub-agent ───────────────────────────────────────────
server = Server("ai-factory-local-bridge")


@server.list_tools()
async def list_tools():
    token = get_okta_token()
    headers = {
        "authorization": f"Bearer {token}",
        # Pass SAP token the same way AgentCore does
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken": token,
    }
    async with streamablehttp_client(LOCAL_AGENT_URL, headers,
                                     timeout=timedelta(seconds=60),
                                     terminate_on_close=False) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.list_tools()
            # Strip outputSchema to prevent Kiro validation errors
            for tool in result.tools:
                if hasattr(tool, "outputSchema"):
                    object.__setattr__(tool, "outputSchema", None)
            return result.tools


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    token = get_okta_token()
    headers = {
        "authorization": f"Bearer {token}",
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken": token,
    }
    async with streamablehttp_client(LOCAL_AGENT_URL, headers,
                                     timeout=timedelta(seconds=120),
                                     terminate_on_close=False) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments=arguments)
            return [types.TextContent(type="text", text=c.text)
                    for c in result.content if hasattr(c, "text")] \
                   or [types.TextContent(type="text", text="No result")]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
