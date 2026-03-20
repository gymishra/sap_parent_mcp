"""
SAP Agent Client - Uses MCP to query SAP via AgentCore Runtime.

Flow:
  1. Authenticate with Okta (Authorization Code flow) → get access token (JWT)
  2. Connect to MCP server on AgentCore Runtime (bearer token for inbound auth)
  3. Call MCP tools → SAP OData → returns data

Okta Config:
    Domain:        trial-1053860.okta.com
    Auth URL:      https://trial-1053860.okta.com/oauth2/v1/authorize
    Token URL:     https://trial-1053860.okta.com/oauth2/v1/token
    Grant Type:    Authorization Code
    Scopes:        openid email
    Callback:      http://localhost:8085/callback

Before running, set these environment variables:
    OKTA_CLIENT_ID      - 0oa10vth79kZAuXGt698
    OKTA_CLIENT_SECRET  - (your secret)
    AWS_DEFAULT_REGION  - AWS region

Usage:
    python sap_agent_client.py "show me top 2 sales orders"
"""
import os
import sys
import json
import asyncio
import base64
import hashlib
import secrets
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

import httpx
import boto3
from datetime import timedelta
from boto3.session import Session

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


# ── Okta Configuration ────────────────────────────────────────────────────────

OKTA_DOMAIN = "trial-1053860.okta.com"
OKTA_AUTHORIZE_URL = f"https://{OKTA_DOMAIN}/oauth2/default/v1/authorize"
OKTA_TOKEN_URL = f"https://{OKTA_DOMAIN}/oauth2/default/v1/token"
OKTA_CLIENT_ID = os.environ.get("OKTA_CLIENT_ID", "0oa10vth79kZAuXGt698")
OKTA_CLIENT_SECRET = os.environ.get("OKTA_CLIENT_SECRET", "")
OKTA_SCOPES = "openid email"
OKTA_CALLBACK_PORT = 8085
OKTA_REDIRECT_URI = f"http://localhost:{OKTA_CALLBACK_PORT}/callback"


# ── Authorization Code Flow with local callback server ─────────────────────────

class OktaCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler to capture the Okta authorization code callback."""

    auth_code = None
    state_received = None

    def do_GET(self, *args, **kwargs):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/callback" and "code" in params:
            OktaCallbackHandler.auth_code = params["code"][0]
            OktaCallbackHandler.state_received = params.get("state", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Authentication successful!</h2>"
                b"<p>You can close this window and return to the terminal.</p>"
                b"</body></html>"
            )
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            error = params.get("error", ["unknown"])[0]
            desc = params.get("error_description", [""])[0]
            self.wfile.write(
                f"<html><body><h2>Error: {error}</h2><p>{desc}</p></body></html>".encode()
            )

    def log_message(self, format, *args):
        pass  # Suppress server logs


def get_okta_token_auth_code_flow() -> str:
    """
    Perform Okta Authorization Code flow:
    1. Start local HTTP server for callback
    2. Open browser to Okta authorize URL
    3. User logs in, Okta redirects to localhost with auth code
    4. Exchange auth code for access token (Basic Auth)
    """
    state = "abc"  # matching your Postman config

    # Build authorize URL
    auth_params = {
        "client_id": OKTA_CLIENT_ID,
        "response_type": "code",
        "scope": OKTA_SCOPES,
        "redirect_uri": OKTA_REDIRECT_URI,
        "state": state,
    }
    authorize_url = f"{OKTA_AUTHORIZE_URL}?{urllib.parse.urlencode(auth_params)}"

    # Start callback server in background
    server = HTTPServer(("localhost", OKTA_CALLBACK_PORT), OktaCallbackHandler)
    server_thread = Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    # Open browser for user login
    print(f"   Opening browser for Okta login...")
    print(f"   URL: {authorize_url}")
    webbrowser.open(authorize_url)

    # Wait for callback
    print(f"   Waiting for Okta callback on port {OKTA_CALLBACK_PORT}...")
    server_thread.join(timeout=120)
    server.server_close()

    if not OktaCallbackHandler.auth_code:
        raise RuntimeError("Failed to receive authorization code from Okta")

    auth_code = OktaCallbackHandler.auth_code
    print(f"   Authorization code received")

    # Exchange auth code for token using Basic Auth (matching Postman config)
    credentials = base64.b64encode(
        f"{OKTA_CLIENT_ID}:{OKTA_CLIENT_SECRET}".encode()
    ).decode()

    with httpx.Client() as client:
        resp = client.post(
            OKTA_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": OKTA_REDIRECT_URI,
            },
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
        token_data = resp.json()
        # access_token for AgentCore inbound auth, id_token for SAP
        return token_data["access_token"], token_data["id_token"]


# ── MCP Client to AgentCore Runtime ────────────────────────────────────────────

async def query_sap_via_mcp(access_token: str, id_token: str, user_query: str):
    """
    Connect to the SAP MCP server on AgentCore Runtime and call tools.
    access_token: for AgentCore inbound auth
    id_token: passed to MCP tools for SAP OData calls
    """
    boto_session = Session()
    region = boto_session.region_name

    # Retrieve Agent ARN from SSM
    ssm_client = boto3.client("ssm", region_name=region)
    agent_arn = ssm_client.get_parameter(
        Name="/sap_mcp_server/agent_arn"
    )["Parameter"]["Value"]
    print(f"   Agent ARN: {agent_arn}")

    # Build MCP URL
    encoded_arn = agent_arn.replace(":", "%3A").replace("/", "%2F")
    mcp_url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com"
        f"/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )

    headers = {
        "authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    print(f"   Connecting to MCP server...")

    async with streamablehttp_client(
        mcp_url, headers, timeout=timedelta(seconds=120), terminate_on_close=False
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # List available tools
            tool_result = await session.list_tools()
            print(f"\n   Available MCP tools:")
            for tool in tool_result.tools:
                print(f"     - {tool.name}: {tool.description}")

            print(f"\n   User query: {user_query}")

            # Route based on query
            if "sales order" in user_query.lower():
                # Extract number if present
                top = 10
                for word in user_query.split():
                    if word.isdigit():
                        top = int(word)
                        break

                print(f"\n   Calling get_sales_orders(top={top})...")
                result = await session.call_tool(
                    "get_sales_orders",
                    arguments={
                        "bearer_token": id_token,
                        "top": top,
                    },
                )

                print("\n" + "=" * 60)
                print(f"SAP Sales Orders (top {top})")
                print("=" * 60)
                for content in result.content:
                    if hasattr(content, "text"):
                        orders = json.loads(content.text)
                        if isinstance(orders, list):
                            for i, order in enumerate(orders, 1):
                                print(f"\n  Order #{i}:")
                                for k, v in order.items():
                                    if v is not None:
                                        print(f"    {k}: {v}")
                        elif "error" in orders:
                            print(f"\n  Error: {orders['error']}")
                        else:
                            print(json.dumps(orders, indent=2))
            else:
                print("   Query not recognized. Try: 'show me top 2 sales orders'")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "show me top 2 sales orders"

    print("=" * 60)
    print("SAP Agent Client - AgentCore Runtime + Okta Auth")
    print("=" * 60)

    # Step 1: Okta Authorization Code flow
    print("\n1. Authenticating with Okta (Authorization Code flow)...")
    if not OKTA_CLIENT_SECRET:
        print("   ERROR: Set OKTA_CLIENT_SECRET environment variable")
        sys.exit(1)
    access_token, id_token = get_okta_token_auth_code_flow()
    print(f"   Access token obtained (length: {len(access_token)})")
    print(f"\n   --- FULL ACCESS TOKEN ---")
    print(access_token)
    print(f"   --- END TOKEN ---")

    # Debug: print token issuer so we can verify discovery URL match
    try:
        payload_part = access_token.split(".")[1]
        payload_part += "=" * (4 - len(payload_part) % 4)
        token_claims = json.loads(base64.b64decode(payload_part))
        print(f"   Token issuer (iss): {token_claims.get('iss')}")
        print(f"   Token audience (aud): {token_claims.get('aud')}")
        print(f"   Token client_id (cid): {token_claims.get('cid')}")
    except Exception as e:
        print(f"   Could not decode token: {e}")

    # Step 2: Call MCP server via AgentCore Runtime
    # Same access_token for AgentCore inbound auth AND SAP OData calls
    print("\n2. Querying SAP via AgentCore MCP Server...")
    asyncio.run(query_sap_via_mcp(access_token, access_token, query))


if __name__ == "__main__":
    main()
