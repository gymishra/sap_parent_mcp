"""
Stdio-to-StreamableHTTP bridge for Kiro.
Kiro spawns this as a local stdio MCP server.
It bridges to the remote AgentCore MCP server via Streamable HTTP with OAuth.
"""
import os, sys, json, base64, time, asyncio, webbrowser, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from datetime import timedelta

import httpx, boto3
from boto3.session import Session
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

OKTA_DOMAIN = os.environ.get("OKTA_DOMAIN", "trial-1053860.okta.com")
OKTA_AUTH_SERVER = os.environ.get("OKTA_AUTH_SERVER", "default")
OKTA_AUTHORIZE_URL = f"https://{OKTA_DOMAIN}/oauth2/{OKTA_AUTH_SERVER}/v1/authorize"
OKTA_TOKEN_URL = f"https://{OKTA_DOMAIN}/oauth2/{OKTA_AUTH_SERVER}/v1/token"
OKTA_CLIENT_ID = os.environ["OKTA_CLIENT_ID"]
OKTA_CLIENT_SECRET = os.environ["OKTA_CLIENT_SECRET"]
OKTA_SCOPES = os.environ.get("OKTA_SCOPES", "openid email")
OKTA_REDIRECT_URI = os.environ.get("OKTA_REDIRECT_URI", "http://localhost:8085/callback")
OKTA_CALLBACK_PORT = int(OKTA_REDIRECT_URI.split(":")[-1].split("/")[0])
AGENTCORE_ARN_SSM_PARAM = os.environ.get("AGENTCORE_ARN_SSM_PARAM", "/sap_mcp_server/agent_arn")
TOKEN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".okta_token_cache.json")

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

def _load_cached_token():
    try:
        with open(TOKEN_CACHE_FILE) as f:
            data = json.load(f)
        token = data.get("access_token", "")
        if token:
            payload = token.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.b64decode(payload))
            if claims.get("exp", 0) > time.time() + 60:
                return token
    except:
        pass
    return None

def _save_token(token):
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump({"access_token": token}, f)

def get_okta_token():
    cached = _load_cached_token()
    if cached:
        return cached
    auth_params = {"client_id": OKTA_CLIENT_ID, "response_type": "code",
        "scope": OKTA_SCOPES, "redirect_uri": OKTA_REDIRECT_URI, "state": "abc"}
    url = f"{OKTA_AUTHORIZE_URL}?{urllib.parse.urlencode(auth_params)}"
    srv = HTTPServer(("localhost", OKTA_CALLBACK_PORT), CallbackHandler)
    CallbackHandler.auth_code = None
    t = Thread(target=srv.handle_request, daemon=True); t.start()
    webbrowser.open(url); t.join(timeout=120); srv.server_close()
    if not CallbackHandler.auth_code:
        raise RuntimeError("No auth code from Okta")
    creds = base64.b64encode(f"{OKTA_CLIENT_ID}:{OKTA_CLIENT_SECRET}".encode()).decode()
    with httpx.Client() as c:
        resp = c.post(OKTA_TOKEN_URL, data={"grant_type": "authorization_code",
            "code": CallbackHandler.auth_code, "redirect_uri": OKTA_REDIRECT_URI},
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"})
        resp.raise_for_status()
        token = resp.json()["access_token"]
        _save_token(token)
        return token

def get_mcp_url():
    boto_session = Session()
    region = boto_session.region_name
    ssm = boto3.client("ssm", region_name=region)
    agent_arn = ssm.get_parameter(Name=AGENTCORE_ARN_SSM_PARAM)["Parameter"]["Value"]
    encoded = agent_arn.replace(":", "%3A").replace("/", "%2F")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded}/invocations?qualifier=DEFAULT"

server = Server("sap-odata-bridge")

@server.list_tools()
async def list_tools():
    """Return hardcoded tool definitions — no remote call needed at startup."""
    return [
        types.Tool(name="get_sales_orders", description="Get sales orders from SAP S/4HANA.",
            inputSchema={"type": "object", "properties": {
                "top": {"type": "integer", "default": 10, "description": "Number of sales orders to return"},
                "skip": {"type": "integer", "default": 0, "description": "Number of records to skip"}}}),
        types.Tool(name="get_sales_order_by_id", description="Get a specific sales order by ID from SAP S/4HANA.",
            inputSchema={"type": "object", "properties": {
                "sales_order_id": {"type": "string", "description": "The sales order number (e.g. '0000000001')"}},
                "required": ["sales_order_id"]}),
    ]

@server.call_tool()
async def call_tool(name, arguments):
    token = get_okta_token()
    arguments["bearer_token"] = token
    mcp_url = get_mcp_url()
    headers = {"authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with streamablehttp_client(mcp_url, headers, timeout=timedelta(seconds=120), terminate_on_close=False) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments=arguments)
            texts = []
            for content in result.content:
                if hasattr(content, "text"):
                    texts.append(types.TextContent(type="text", text=content.text))
            if not texts:
                texts.append(types.TextContent(type="text", text="No result"))
            return texts

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
