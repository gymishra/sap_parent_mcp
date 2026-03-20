"""
Test: Python → Gateway → Runtime → SAP using Okta JWT (3LO).

Flow:
  1. User authenticates with Okta (Authorization Code / 3LO)
  2. Gets JWT with gyanmis@amazon.com identity
  3. Sends JWT to Gateway (inbound auth validates it)
  4. Gateway forwards to MCP Server Runtime
  5. MCP Server calls SAP with the token
  6. Sales orders returned

Usage:
    $env:OKTA_CLIENT_SECRET="your-secret"
    python test_gateway.py
"""
import os
import sys
import json
import asyncio
import base64
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# ── Config ─────────────────────────────────────────────────────────────────────
OKTA_DOMAIN = "trial-1053860.okta.com"
OKTA_AUTHORIZE_URL = f"https://{OKTA_DOMAIN}/oauth2/default/v1/authorize"
OKTA_TOKEN_URL = f"https://{OKTA_DOMAIN}/oauth2/default/v1/token"
OKTA_CLIENT_ID = "0oa10vth79kZAuXGt698"
OKTA_CLIENT_SECRET = os.environ.get("OKTA_CLIENT_SECRET", "")
OKTA_REDIRECT_URI = "http://localhost:8085/callback"

GATEWAY_ID = "gateway-quick-start-3feaa5-1anyuhb64c"
GATEWAY_URL = f"https://{GATEWAY_ID}.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"

# ── Okta 3LO Auth ─────────────────────────────────────────────────────────────

class CallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    def do_GET(self, *a, **k):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authenticated! Return to terminal.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
    def log_message(self, *a): pass


def get_okta_token() -> str:
    if not OKTA_CLIENT_SECRET:
        print("ERROR: Set OKTA_CLIENT_SECRET env var")
        sys.exit(1)

    auth_params = {
        "client_id": OKTA_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email",
        "redirect_uri": OKTA_REDIRECT_URI,
        "state": "abc",
    }
    url = f"{OKTA_AUTHORIZE_URL}?{urllib.parse.urlencode(auth_params)}"

    server = HTTPServer(("localhost", 8085), CallbackHandler)
    CallbackHandler.auth_code = None
    t = Thread(target=server.handle_request, daemon=True)
    t.start()
    print("Opening browser for Okta login...")
    webbrowser.open(url)
    t.join(timeout=120)
    server.server_close()

    if not CallbackHandler.auth_code:
        raise RuntimeError("No auth code from Okta")

    creds = base64.b64encode(f"{OKTA_CLIENT_ID}:{OKTA_CLIENT_SECRET}".encode()).decode()
    with httpx.Client() as c:
        resp = c.post(
            OKTA_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": CallbackHandler.auth_code,
                "redirect_uri": OKTA_REDIRECT_URI,
            },
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

# ── Test Gateway ───────────────────────────────────────────────────────────────

async def test_gateway(token: str):
    print(f"\nGateway URL: {GATEWAY_URL}")
    headers = {
        "authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    print("Connecting to Gateway MCP server...")
    try:
        async with streamablehttp_client(
            GATEWAY_URL, headers, timeout=timedelta(seconds=120), terminate_on_close=False
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("Session initialized")

                tools = await session.list_tools()
                print(f"\nAvailable tools:")
                tool_names = []
                for t in tools.tools:
                    print(f"  - {t.name}: {t.description}")
                    tool_names.append(t.name)

                # Find the sales orders tool (Gateway may prefix with target name)
                sales_tool = None
                for name in tool_names:
                    if "get_sales_orders" in name:
                        sales_tool = name
                        break

                if not sales_tool:
                    print(f"\nNo get_sales_orders tool found. Available: {tool_names}")
                    return

                print(f"\nCalling {sales_tool}(top=2)...")
                result = await session.call_tool(
                    sales_tool,
                    arguments={"bearer_token": token, "top": 2},
                )
                print("\n--- SAP Sales Orders ---")
                for content in result.content:
                    if hasattr(content, "text"):
                        print(content.text)
    except Exception as e:
        print(f"Error: {e}")


def main():
    print("=" * 60)
    print("Test: Python → Gateway → Runtime → SAP (JWT/3LO)")
    print("=" * 60)

    print("\n1. Authenticating with Okta (3LO)...")
    token = get_okta_token()

    # Show token claims
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.b64decode(payload))
        print(f"   iss: {claims.get('iss')}")
        print(f"   sub: {claims.get('sub')}")
        print(f"   cid: {claims.get('cid')}")
    except:
        pass

    print("\n2. Calling Gateway...")
    asyncio.run(test_gateway(token))


if __name__ == "__main__":
    main()
