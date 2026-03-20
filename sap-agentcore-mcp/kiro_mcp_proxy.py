"""
Local MCP proxy for Kiro — connects to SAP MCP server on AgentCore Runtime.

This runs as a local stdio MCP server that Kiro can connect to.
It proxies tool calls to the remote AgentCore MCP server with Okta auth.

Usage in Kiro mcp.json:
{
  "mcpServers": {
    "sap-odata": {
      "command": "python",
      "args": ["sap-agentcore-mcp/kiro_mcp_proxy.py"],
      "env": {
        "OKTA_CLIENT_SECRET": "your-secret"
      }
    }
  }
}
"""
import os
import sys
import json
import base64
import asyncio
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from datetime import timedelta

import httpx
import boto3
from boto3.session import Session
from mcp.server.fastmcp import FastMCP
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Okta config
OKTA_DOMAIN = "trial-1053860.okta.com"
OKTA_AUTHORIZE_URL = f"https://{OKTA_DOMAIN}/oauth2/default/v1/authorize"
OKTA_TOKEN_URL = f"https://{OKTA_DOMAIN}/oauth2/default/v1/token"
OKTA_CLIENT_ID = "0oa10vth79kZAuXGt698"
OKTA_CLIENT_SECRET = os.environ.get("OKTA_CLIENT_SECRET", "")
OKTA_REDIRECT_URI = "http://localhost:8085/callback"

# Local MCP server (stdio for Kiro)
proxy = FastMCP("SAP OData Proxy")

# Cache token
_cached_token = None


class CallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    def do_GET(self, *a, **k):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authenticated! Return to Kiro.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
    def log_message(self, *a): pass


def get_okta_token() -> str:
    global _cached_token
    if _cached_token:
        # Check if expired
        try:
            payload = _cached_token.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.b64decode(payload))
            import time
            if claims.get("exp", 0) > time.time() + 60:
                return _cached_token
        except:
            pass

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
    webbrowser.open(url)
    t.join(timeout=120)
    server.server_close()

    if not CallbackHandler.auth_code:
        raise RuntimeError("No auth code received from Okta")

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
        _cached_token = resp.json()["access_token"]
        return _cached_token


@proxy.tool()
def get_sales_orders(top: int = 10, skip: int = 0) -> str:
    """
    Get sales orders from SAP S/4HANA.

    Args:
        top: Number of sales orders to return (default 10)
        skip: Number of records to skip for pagination (default 0)

    Returns:
        JSON string with sales order data
    """
    token = get_okta_token()
    result = _call_remote_tool("get_sales_orders", {
        "bearer_token": token,
        "top": top,
        "skip": skip,
    })
    return result


@proxy.tool()
def get_sales_order_by_id(sales_order_id: str) -> str:
    """
    Get a specific sales order by ID from SAP S/4HANA.

    Args:
        sales_order_id: The sales order number (e.g. '0000000001')

    Returns:
        JSON string with sales order details
    """
    token = get_okta_token()
    result = _call_remote_tool("get_sales_order_by_id", {
        "bearer_token": token,
        "sales_order_id": sales_order_id,
    })
    return result


def _call_remote_tool(tool_name: str, arguments: dict) -> str:
    """Call a tool on the remote AgentCore MCP server."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(asyncio.run, _call_remote_tool_async(tool_name, arguments)).result()
            return result
    else:
        return asyncio.run(_call_remote_tool_async(tool_name, arguments))


async def _call_remote_tool_async(tool_name: str, arguments: dict) -> str:
    boto_session = Session()
    region = boto_session.region_name

    ssm_client = boto3.client("ssm", region_name=region)
    agent_arn = ssm_client.get_parameter(
        Name="/sap_mcp_server/agent_arn"
    )["Parameter"]["Value"]

    encoded_arn = agent_arn.replace(":", "%3A").replace("/", "%2F")
    mcp_url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com"
        f"/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )

    token = arguments.get("bearer_token", "")
    headers = {
        "authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with streamablehttp_client(
        mcp_url, headers, timeout=timedelta(seconds=120), terminate_on_close=False
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)
            for content in result.content:
                if hasattr(content, "text"):
                    return content.text
    return "No result"


if __name__ == "__main__":
    proxy.run(transport="stdio")
