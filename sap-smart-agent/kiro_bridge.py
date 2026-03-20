"""Kiro stdio bridge for SAP Smart Agent — same pattern as the original."""
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
OKTA_REDIRECT_URI = os.environ.get("OKTA_REDIRECT_URI", "http://localhost:8086/callback")
OKTA_CALLBACK_PORT = int(OKTA_REDIRECT_URI.split(":")[-1].split("/")[0])
SSM_PARAM = os.environ.get("AGENTCORE_ARN_SSM_PARAM", "/sap_smart_agent/agent_arn")
TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".okta_token_cache.json")

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
    try:
        with open(TOKEN_CACHE) as f:
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

server = Server("sap-smart-bridge")

@server.list_tools()
async def list_tools():
    return [
        types.Tool(name="call_adt_api",description="Call SAP ADT REST API for ABAP development. Single tool for: search objects, read/write source code, lock/unlock, activate, syntax check, unit tests, SQL queries, transports, DDIC access.",
            inputSchema={"type":"object","properties":{
                "adt_path":{"type":"string","description":"ADT REST path (e.g. '/sap/bc/adt/programs/programs/SAPMV45A/source/main')"},
                "method":{"type":"string","default":"GET","description":"HTTP method: GET, POST, PUT, DELETE"},
                "query_params":{"type":"string","default":"","description":"URL query string"},
                "body":{"type":"string","default":"","description":"Request body for POST/PUT (source code, XML, SQL)"},
                "content_type":{"type":"string","default":"","description":"Content-Type override"},
                "accept":{"type":"string","default":"","description":"Accept header override (e.g. 'application/vnd.sap.as+xml;charset=UTF-8;dataname=com.sap.adt.lock.result')"},
                "bearer_token":{"type":"string","default":""}
            },"required":["adt_path"]}),
        types.Tool(name="discover_sap_services",description="Discover all available OData services from SAP catalog.",
            inputSchema={"type":"object","properties":{"bearer_token":{"type":"string","default":""}}}),
        types.Tool(name="get_service_metadata",description="Get entity types and properties for a SAP OData service.",
            inputSchema={"type":"object","properties":{
                "service_path":{"type":"string","description":"OData service path"},
                "bearer_token":{"type":"string","default":""}
            },"required":["service_path"]}),
        types.Tool(name="query_sap_odata",description="Query any SAP OData entity set.",
            inputSchema={"type":"object","properties":{
                "service_path":{"type":"string","description":"OData service path"},
                "entity_set":{"type":"string","description":"Entity set name"},
                "top":{"type":"integer","default":10},
                "skip":{"type":"integer","default":0},
                "filter_expr":{"type":"string","default":"","description":"OData $filter"},
                "select_fields":{"type":"string","default":"","description":"Comma-separated fields"},
                "bearer_token":{"type":"string","default":""}
            },"required":["service_path","entity_set"]}),
        types.Tool(name="get_sap_entity",description="Get a single SAP entity by key.",
            inputSchema={"type":"object","properties":{
                "service_path":{"type":"string"},
                "entity_set":{"type":"string"},
                "key":{"type":"string","description":"Entity key value"},
                "bearer_token":{"type":"string","default":""}
            },"required":["service_path","entity_set","key"]}),
        types.Tool(name="update_sap_entity",description="Update a SAP entity via PATCH. Handles CSRF token and ETag automatically.",
            inputSchema={"type":"object","properties":{
                "service_path":{"type":"string","description":"OData service path"},
                "entity_set":{"type":"string","description":"Entity set name"},
                "key":{"type":"string","description":"Entity key expression (e.g. \"SalesOrder='2',SalesOrderItem='10'\")"},
                "payload":{"type":"string","description":"JSON string with fields to update"},
                "bearer_token":{"type":"string","default":""}
            },"required":["service_path","entity_set","key","payload"]}),
        types.Tool(name="create_sap_entity",description="Create a new SAP entity via POST. Handles CSRF token automatically.",
            inputSchema={"type":"object","properties":{
                "service_path":{"type":"string","description":"OData service path"},
                "entity_set":{"type":"string","description":"Entity set name"},
                "payload":{"type":"string","description":"JSON string with entity data to create"},
                "bearer_token":{"type":"string","default":""}
            },"required":["service_path","entity_set","payload"]}),
        types.Tool(name="generate_and_deploy_mcp_server",
            description="Generate a new focused SAP MCP server and deploy it to AgentCore. Discovers relevant SAP OData services automatically, generates tool code using Claude, and deploys a new AgentCore runtime with the same Okta auth.",
            inputSchema={"type":"object","properties":{
                "prompt":{"type":"string","description":"Description of what the MCP server should do (e.g. 'Plant Maintenance tools for orders and notifications')"},
                "agent_name":{"type":"string","description":"Short identifier for the new server (e.g. 'sap_pm_agent'). Must be lowercase with underscores."},
                "bearer_token":{"type":"string","default":""}
            },"required":["prompt","agent_name"]}),
    ]

@server.call_tool()
async def call_tool(name, arguments):
    token = get_okta_token()
    arguments["bearer_token"] = token  # AgentCore doesn't forward auth header into tool context
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
