"""Kiro stdio bridge for SAP Sales Order Agent on AgentCore."""
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
OKTA_REDIRECT_URI = os.environ.get("OKTA_REDIRECT_URI", "http://localhost:8087/callback")
OKTA_CALLBACK_PORT = int(OKTA_REDIRECT_URI.split(":")[-1].split("/")[0])
SSM_PARAM = os.environ.get("AGENTCORE_ARN_SSM_PARAM", "/sap_generated/sap_sales_order_agent/agent_arn")
TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".okta_token_cache.json")
SMART_AGENT_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "sap-smart-agent", ".okta_token_cache.json")

# ── Common filter schema for reuse ───────────────────────────────────────────
FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "top": {"type": "integer", "default": 10, "description": "Max results to return"},
        "skip": {"type": "integer", "default": 0, "description": "Results to skip"},
        "filter_expr": {"type": "string", "default": "", "description": "OData $filter expression"},
        "bearer_token": {"type": "string", "default": ""}
    }
}

class CallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    def do_GET(self, *a, **k):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            CallbackHandler.auth_code = params["code"][0]
            self.send_response(200); self.send_header("Content-Type","text/html"); self.end_headers()
            self.wfile.write(b"<h2>Authenticated! Return to Kiro.</h2>")
        else:
            self.send_response(400); self.end_headers()
    def log_message(self, *a): pass

def _load_cached_token():
    for path in [TOKEN_CACHE, SMART_AGENT_CACHE]:
        try:
            with open(path) as f:
                token = json.load(f).get("access_token","")
            if token:
                p = token.split(".")[1]; p += "=" * (4-len(p)%4)
                if json.loads(base64.b64decode(p)).get("exp",0) > time.time()+60:
                    return token
        except: pass
    return None

def _save_token(token):
    with open(TOKEN_CACHE,"w") as f: json.dump({"access_token":token},f)

def get_okta_token():
    cached = _load_cached_token()
    if cached: return cached
    params = {"client_id":OKTA_CLIENT_ID,"response_type":"code","scope":OKTA_SCOPES,
              "redirect_uri":OKTA_REDIRECT_URI,"state":"abc"}
    url = f"{OKTA_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    srv = HTTPServer(("localhost",OKTA_CALLBACK_PORT),CallbackHandler)
    CallbackHandler.auth_code = None
    t = Thread(target=srv.handle_request,daemon=True); t.start()
    webbrowser.open(url); t.join(timeout=120); srv.server_close()
    if not CallbackHandler.auth_code: raise RuntimeError("No auth code")
    creds = base64.b64encode(f"{OKTA_CLIENT_ID}:{OKTA_CLIENT_SECRET}".encode()).decode()
    with httpx.Client() as c:
        r = c.post(OKTA_TOKEN_URL,data={"grant_type":"authorization_code",
            "code":CallbackHandler.auth_code,"redirect_uri":OKTA_REDIRECT_URI},
            headers={"Authorization":f"Basic {creds}","Content-Type":"application/x-www-form-urlencoded"})
        r.raise_for_status(); token = r.json()["access_token"]; _save_token(token); return token

def get_mcp_url():
    s = Session(); ssm = boto3.client("ssm",region_name=s.region_name)
    arn = ssm.get_parameter(Name=SSM_PARAM)["Parameter"]["Value"]
    enc = arn.replace(":", "%3A").replace("/", "%2F")
    return f"https://bedrock-agentcore.{s.region_name}.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT"

server = Server("sap-sales-order-bridge")

@server.list_tools()
async def list_tools():
    return [
        types.Tool(name="get_sales_orders",
            description="Query sales orders with filtering and pagination. Filter examples: \"SalesOrder eq '2'\", \"SoldToParty eq '17100003'\", \"TotalNetAmount gt 1000\"",
            inputSchema=FILTER_SCHEMA),
        types.Tool(name="get_sales_order_partners",
            description="Get sales order partners (sold-to, ship-to, bill-to, payer). Filter by sales order number.",
            inputSchema=FILTER_SCHEMA),
        types.Tool(name="get_sales_order_pricing_elements",
            description="Get pricing breakdown (discounts, surcharges, taxes, conditions) for sales orders.",
            inputSchema=FILTER_SCHEMA),
        types.Tool(name="get_sales_order_billing_plans",
            description="Get billing plans and schedules for sales orders.",
            inputSchema=FILTER_SCHEMA),
        types.Tool(name="get_sales_order_billing_plan_items",
            description="Get detailed billing plan items including dates, amounts and billing status.",
            inputSchema=FILTER_SCHEMA),
    ]

@server.call_tool()
async def call_tool(name, arguments):
    token = get_okta_token()
    arguments["bearer_token"] = token
    mcp_url = get_mcp_url()
    headers = {"authorization":f"Bearer {token}","Content-Type":"application/json"}
    async with streamablehttp_client(mcp_url,headers,timeout=timedelta(seconds=120),terminate_on_close=False) as (r,w,_):
        async with ClientSession(r,w) as session:
            await session.initialize()
            result = await session.call_tool(name,arguments=arguments)
            return [types.TextContent(type="text",text=c.text) for c in result.content if hasattr(c,"text")] or [types.TextContent(type="text",text="No result")]

async def main():
    async with stdio_server() as (r,w):
        await server.run(r,w,server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
