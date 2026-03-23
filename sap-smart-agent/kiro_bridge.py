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
                "accept":{"type":"string","default":"","description":"Accept header override (e.g. 'application/vnd.sap.as+xml;charset=UTF-8;dataname=com.sap.adt.lock.result')"}
            },"required":["adt_path"]}),
        types.Tool(name="upload_abap_source",
            description="Upload ABAP source code to SAP: creates program if needed, then lock → write → unlock → activate all in one session. Solves the lock-handle problem.",
            inputSchema={"type":"object","properties":{
                "program_name":{"type":"string","description":"ABAP program name (e.g. 'Z_INVOICE_3WAY_MATCH')"},
                "source_code":{"type":"string","description":"Full ABAP source code to upload"},
                "description":{"type":"string","default":"","description":"Program description (for new programs)"},
                "activate":{"type":"boolean","default":True,"description":"Whether to activate after upload"}
            },"required":["program_name","source_code"]}),
        types.Tool(name="discover_sap_services",description="Discover all available OData services from SAP catalog.",
            inputSchema={"type":"object","properties":{}}),
        types.Tool(name="get_service_metadata",description="Get entity types and properties for a SAP OData service.",
            inputSchema={"type":"object","properties":{
                "service_path":{"type":"string","description":"OData service path"}
            },"required":["service_path"]}),
        types.Tool(name="query_sap_odata",description="Query any SAP OData entity set.",
            inputSchema={"type":"object","properties":{
                "service_path":{"type":"string","description":"OData service path"},
                "entity_set":{"type":"string","description":"Entity set name"},
                "top":{"type":"integer","default":10},
                "skip":{"type":"integer","default":0},
                "filter_expr":{"type":"string","default":"","description":"OData $filter"},
                "select_fields":{"type":"string","default":"","description":"Comma-separated fields"}
            },"required":["service_path","entity_set"]}),
        types.Tool(name="get_sap_entity",description="Get a single SAP entity by key.",
            inputSchema={"type":"object","properties":{
                "service_path":{"type":"string"},
                "entity_set":{"type":"string"},
                "key":{"type":"string","description":"Entity key value"}
            },"required":["service_path","entity_set","key"]}),
        types.Tool(name="update_sap_entity",description="Update a SAP entity via PATCH. Handles CSRF token and ETag automatically.",
            inputSchema={"type":"object","properties":{
                "service_path":{"type":"string","description":"OData service path"},
                "entity_set":{"type":"string","description":"Entity set name"},
                "key":{"type":"string","description":"Entity key expression (e.g. \"SalesOrder='2',SalesOrderItem='10'\")"},
                "payload":{"type":"string","description":"JSON string with fields to update"}
            },"required":["service_path","entity_set","key","payload"]}),
        types.Tool(name="create_sap_entity",description="Create a new SAP entity via POST. Handles CSRF token automatically.",
            inputSchema={"type":"object","properties":{
                "service_path":{"type":"string","description":"OData service path"},
                "entity_set":{"type":"string","description":"Entity set name"},
                "payload":{"type":"string","description":"JSON string with entity data to create"}
            },"required":["service_path","entity_set","payload"]}),
        types.Tool(name="search_sap_services",
            description="Search SAP services by keyword. Returns only matching services from in-memory cache. Much faster and lighter than discover_sap_services. Use this INSTEAD of discover_sap_services.",
            inputSchema={"type":"object","properties":{
                "keyword":{"type":"string","description":"Search term (e.g. 'sales', 'material', 'purchase')"},
                "limit":{"type":"integer","default":20}
            },"required":["keyword"]}),
        types.Tool(name="generate_and_deploy_mcp_server",
            description="Generate a new focused SAP MCP server and deploy it to AgentCore. Discovers relevant SAP OData services automatically, generates tool code using Claude, and deploys a new AgentCore runtime with the same Okta auth.",
            inputSchema={"type":"object","properties":{
                "prompt":{"type":"string","description":"Description of what the MCP server should do (e.g. 'Plant Maintenance tools for orders and notifications')"},
                "agent_name":{"type":"string","description":"Short identifier for the new server (e.g. 'sap_pm_agent'). Must be lowercase with underscores."}
            },"required":["prompt","agent_name"]}),
        # ── Smart Query (NL → SQL) ──
        types.Tool(name="smart_query",
            description="Answer a natural language question about SAP data. Auto-decides between OData and SQL. Uses OData when a matching service exists (simple reads), falls back to SQL for JOINs, aggregations, raw tables, or when no OData service covers the data. Returns structured results with the method used and reasoning.",
            inputSchema={"type":"object","properties":{
                "question":{"type":"string","description":"Natural language question about SAP data"},
                "max_rows":{"type":"integer","default":100,"description":"Max rows to return"},
                "prefer":{"type":"string","default":"auto","description":"'auto' (AI decides), 'odata' (force OData), 'sql' (force SQL)"}
            },"required":["question"]}),
        # ── Create OData Service ──
        types.Tool(name="create_odata_service",
            description="Create a new OData service in SAP by generating and deploying a CDS view with @OData.publish. Uses AI to generate the CDS source from a natural language description.",
            inputSchema={"type":"object","properties":{
                "description":{"type":"string","description":"What data to expose (e.g. 'Purchase orders with vendor info and item details')"},
                "cds_name":{"type":"string","default":"","description":"Optional CDS view name (auto-generated if empty, must start with Z)"}
            },"required":["description"]}),
        # ── OData Service Activation ──
        types.Tool(name="activate_odata_service",
            description="Activate an OData service in SAP Gateway (equivalent to /IWFND/MAINT_SERVICE). Finds the service in the backend catalog and registers it in the frontend hub.",
            inputSchema={"type":"object","properties":{
                "service_name":{"type":"string","description":"Technical service name (e.g. 'API_SALES_ORDER_SRV')"},
                "service_version":{"type":"string","default":"0001","description":"Service version"},
                "system_alias":{"type":"string","default":"LOCAL","description":"System alias for backend"}
            },"required":["service_name"]}),
        types.Tool(name="list_backend_services",
            description="List OData services in the SAP backend catalog (registered but possibly not activated). Use to find services that can be activated.",
            inputSchema={"type":"object","properties":{
                "search":{"type":"string","default":"","description":"Search term (e.g. 'SALES', 'MATERIAL')"},
                "max_results":{"type":"integer","default":20}
            },"required":[]}),
        # ── Convenience ADT Tools ──
        types.Tool(name="get_abap_program",
            description="Read ABAP program/report source code from SAP.",
            inputSchema={"type":"object","properties":{
                "program_name":{"type":"string","description":"Program name (e.g. 'Z_INVOICE_3WAY_MATCH', 'SAPMV45A')"}
            },"required":["program_name"]}),
        types.Tool(name="get_abap_class",
            description="Read ABAP class source code from SAP.",
            inputSchema={"type":"object","properties":{
                "class_name":{"type":"string","description":"Class name (e.g. 'ZCL_MY_CLASS', 'CL_ABAP_TYPEDESCR')"},
                "include":{"type":"string","default":"main","description":"Part to read: main, definitions, implementations, testclasses"}
            },"required":["class_name"]}),
        types.Tool(name="get_function_module",
            description="Read function module source code from SAP.",
            inputSchema={"type":"object","properties":{
                "function_group":{"type":"string","description":"Function group name (e.g. 'SMOD')"},
                "function_name":{"type":"string","description":"Function module name (e.g. 'BAPI_MATERIAL_GET_DETAIL')"}
            },"required":["function_group","function_name"]}),
        types.Tool(name="get_abap_interface",
            description="Read ABAP interface source code from SAP.",
            inputSchema={"type":"object","properties":{
                "interface_name":{"type":"string","description":"Interface name (e.g. 'ZIF_MY_INTERFACE')"}
            },"required":["interface_name"]}),
        types.Tool(name="get_abap_include",
            description="Read ABAP include program source code from SAP.",
            inputSchema={"type":"object","properties":{
                "include_name":{"type":"string","description":"Include name (e.g. 'MV45AF0B_BELEG_SICHERN')"}
            },"required":["include_name"]}),
        types.Tool(name="search_objects",
            description="Search SAP repository objects (programs, classes, tables, function modules, transactions, etc.).",
            inputSchema={"type":"object","properties":{
                "query":{"type":"string","description":"Search term with wildcards (e.g. 'Z_INVOICE*', 'CL_ABAP*')"},
                "object_type":{"type":"string","default":"","description":"Filter: PROG, CLAS, INTF, FUGR, FUNC, TABL, DTEL, DOMA, TRAN, DEVC"},
                "max_results":{"type":"integer","default":50}
            },"required":["query"]}),
        types.Tool(name="get_package",
            description="Get SAP package (development class) information and contents.",
            inputSchema={"type":"object","properties":{
                "package_name":{"type":"string","description":"Package name (e.g. '$TMP', 'ZPACKAGE')"}
            },"required":["package_name"]}),
        types.Tool(name="get_transaction",
            description="Look up a SAP transaction code and find the associated program/object.",
            inputSchema={"type":"object","properties":{
                "tcode":{"type":"string","description":"Transaction code (e.g. 'VA01', 'MM01', 'SE38')"}
            },"required":["tcode"]}),
        # ── DDIC Tools ──
        types.Tool(name="get_table_definition",
            description="Get DDIC table or structure definition (fields, types, keys) from SAP.",
            inputSchema={"type":"object","properties":{
                "table_name":{"type":"string","description":"Table or structure name (e.g. 'MARA', 'VBAK', 'EKKO')"}
            },"required":["table_name"]}),
        types.Tool(name="get_table_contents",
            description="Execute a SQL query on SAP database tables via ADT Data Preview.",
            inputSchema={"type":"object","properties":{
                "sql_query":{"type":"string","description":"SQL SELECT statement (e.g. \"SELECT * FROM MARA WHERE MATNR = 'TG14'\")"},
                "max_rows":{"type":"integer","default":100}
            },"required":["sql_query"]}),
        types.Tool(name="get_type_info",
            description="Get DDIC type information (data element, domain, or type details).",
            inputSchema={"type":"object","properties":{
                "type_name":{"type":"string","description":"Type name (e.g. 'MATNR', 'BUKRS', 'VBELN')"}
            },"required":["type_name"]}),
        # ── Transport Management ──
        types.Tool(name="create_transport",
            description="Create a new transport request in SAP.",
            inputSchema={"type":"object","properties":{
                "description":{"type":"string","description":"Transport description"},
                "target_system":{"type":"string","default":"","description":"Target system SID (e.g. 'QAS')"},
                "transport_type":{"type":"string","default":"K","description":"'K' workbench, 'W' customizing"}
            },"required":["description"]}),
        types.Tool(name="release_transport",
            description="Release a transport request or task in SAP.",
            inputSchema={"type":"object","properties":{
                "transport_number":{"type":"string","description":"Transport number (e.g. 'DEVK900123')"}
            },"required":["transport_number"]}),
        types.Tool(name="list_user_transports",
            description="List transport requests for a user.",
            inputSchema={"type":"object","properties":{
                "user":{"type":"string","default":"","description":"SAP username (empty = current user)"},
                "status":{"type":"string","default":"D","description":"'D' modifiable, 'R' released, 'N' not released"}
            },"required":[]}),
        # ── Code Quality ──
        types.Tool(name="syntax_check",
            description="Run ABAP syntax check on a program or class.",
            inputSchema={"type":"object","properties":{
                "object_url":{"type":"string","description":"ADT object URI (e.g. '/sap/bc/adt/programs/programs/z_my_program')"},
                "source_code":{"type":"string","default":"","description":"Optional source to check (empty = check saved version)"}
            },"required":["object_url"]}),
        types.Tool(name="run_atc_check",
            description="Run ATC (ABAP Test Cockpit) code quality check on an object.",
            inputSchema={"type":"object","properties":{
                "object_uri":{"type":"string","description":"ADT object URI (e.g. '/sap/bc/adt/programs/programs/z_my_program')"},
                "check_variant":{"type":"string","default":"DEFAULT","description":"ATC check variant"}
            },"required":["object_uri"]}),
        # ── ADT Discovery ──
        types.Tool(name="adt_discovery",
            description="Get the ADT discovery document — lists all available ADT REST API endpoints and capabilities on this SAP system. Useful for finding what operations are supported.",
            inputSchema={"type":"object","properties":{},"required":[]}),
    ]

@server.call_tool()
async def call_tool(name, arguments):
    token = get_okta_token()
    mcp_url = get_mcp_url()
    headers = {
        "authorization": f"Bearer {token}",
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken": token,
        "Content-Type": "application/json"
    }
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
