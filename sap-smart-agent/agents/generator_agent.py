"""
SAP MCP Generator Agent — generates and deploys focused SAP MCP servers to AgentCore.

Detects domain from prompt (S/4HANA OData, Cloud ALM, SuccessFactors) and:
1. Discovers relevant APIs / services
2. Generates Python FastMCP server code via Claude (claude-sonnet-4-6)
3. Uploads to S3 + triggers CodeBuild → deploys to AgentCore Runtime

Port: 8105
Model: us.anthropic.claude-sonnet-4-6 (code generation needs full reasoning)
"""
import os, sys, json, logging, uuid, httpx, xml.etree.ElementTree as ET
import boto3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP, Context
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generator_agent")

MODEL_ID     = "us.anthropic.claude-sonnet-4-6"
SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
CATALOG_URL  = "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"

mcp = FastMCP("AI Factory — Generator Agent", host="0.0.0.0", port=8105, stateless_http=True)
logger = logging.getLogger("ai_factory_generator")

# ── Domain detection ──────────────────────────────────────────────────────────
_CALM_KEYWORDS = {"cloud alm", "cloudalm", "calm", "alm monitoring", "alm project",
                  "alm task", "alm feature", "process monitoring", "alm analytics",
                  "alm document", "test management", "process hierarchy"}
_SF_KEYWORDS   = {"successfactors", "success factors", "sfsf", "hcm", "employee central",
                  "payroll", "recruiting", "learning", "performance", "compensation",
                  "workforce", "onboarding", "succession"}

def _detect_domain(prompt: str) -> str:
    p = prompt.lower()
    if any(k in p for k in _CALM_KEYWORDS): return "calm"
    if any(k in p for k in _SF_KEYWORDS):   return "sf"
    return "s4"


# ── Token helper ──────────────────────────────────────────────────────────────
def _get_token(ctx: Context) -> str:
    try:
        req = ctx.request_context.request
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken", "authorization"]:
            val = req.headers.get(h, "")
            if val:
                return val.replace("Bearer ", "").replace("bearer ", "") \
                    if val.lower().startswith("bearer ") else val
    except Exception: pass
    return os.environ.get("SAP_BEARER_TOKEN", "")


# ── S/4 service discovery helpers ────────────────────────────────────────────
def _sap_get(path: str, token: str, params: dict = None) -> dict:
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                  params=params or {})
        r.raise_for_status()
        return r.json()

def _sap_get_xml(path: str, token: str) -> str:
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})
        r.raise_for_status()
        return r.text

def _discover_relevant_services(token: str, keywords: list) -> list:
    try:
        services = _sap_get(CATALOG_URL, token, {"$format": "json"}).get("d", {}).get("results", [])
        matched = []
        for svc in services:
            title = (svc.get("Title","") + svc.get("TechnicalServiceName","") +
                     svc.get("Description","")).lower()
            if any(kw.lower() in title for kw in keywords):
                matched.append({"Title": svc.get("Title",""),
                                 "TechnicalServiceName": svc.get("TechnicalServiceName","")})
        api = [s for s in matched if s["Title"].startswith("API_")]
        std = [s for s in matched if not s["TechnicalServiceName"].startswith("Z") and not s["Title"].startswith("API_")]
        z   = [s for s in matched if s["TechnicalServiceName"].startswith("Z") and not s["Title"].startswith("API_")]
        return (api + std + z)[:10]
    except Exception as e:
        logger.error(f"Service discovery failed: {e}"); return []

def _get_entities_for_service(service_path: str, token: str) -> list:
    try:
        root = ET.fromstring(_sap_get_xml(f"{service_path}/$metadata", token))
        entities = []
        for ns in ["http://schemas.microsoft.com/ado/2008/09/edm",
                   "http://schemas.microsoft.com/ado/2009/11/edm",
                   "http://docs.oasis-open.org/odata/ns/edm"]:
            for et in root.iter(f"{{{ns}}}EntityType"):
                name = et.get("Name","")
                if name and not name.startswith("I_") and not name.startswith("SAP__"):
                    entities.append(name)
        return entities[:5]
    except Exception as e:
        logger.error(f"Entity fetch failed for {service_path}: {e}"); return []


# ── Bedrock code generation ───────────────────────────────────────────────────
def _generate_s4_tools(prompt: str, services_with_entities: list, agent_name: str) -> str:
    bedrock = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name)
    system = (
        "You are an expert SAP developer. Generate Python MCP tool functions for a FastMCP server.\n"
        "CRITICAL: Entity type names end in 'Type' — strip it for the URL path.\n"
        "  e.g. A_SalesOrderType → use A_SalesOrder in the URL\n\n"
        "Use ONLY this pattern:\n"
        "@mcp.tool()\n"
        "def func_name(ctx: Context, top: int = 10, skip: int = 0, filter_expr: str = \"\") -> str:\n"
        "    \"\"\"docstring\"\"\"\n"
        "    token = _get_token(ctx)\n"
        "    if not token: return json.dumps({\"error\": \"No bearer token\"})\n"
        "    try:\n"
        "        params = {\"$format\": \"json\", \"$top\": str(top), \"$skip\": str(skip)}\n"
        "        if filter_expr: params[\"$filter\"] = filter_expr\n"
        "        data = _sap_get(\"<service_path>/<entity_set>\", token, params)\n"
        "        results = data.get(\"d\", {}).get(\"results\", [])\n"
        "        return json.dumps({\"count\": len(results), \"results\": results}, indent=2)\n"
        "    except Exception as e:\n"
        "        return json.dumps({\"error\": str(e)})\n\n"
        "Return ONLY function definitions, no imports, no main block."
    )
    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 4096,
                         "system": system,
                         "messages": [{"role": "user", "content":
                             f"Request: {prompt}\n\nServices:\n{json.dumps(services_with_entities, indent=2)}\n\n"
                             f"Generate 3-7 MCP tools for this domain."}]}),
        contentType="application/json", accept="application/json")
    return json.loads(resp["body"].read())["content"][0]["text"]

def _generate_calm_tools(prompt: str) -> str:
    bedrock = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name)
    calm_apis = {
        "projects":   "/api/calm-projects/v1/projects",
        "tasks":      "/api/calm-tasks/v1/projects/{project_id}/tasks",
        "features":   "/api/calm-features/v1/Features",
        "documents":  "/api/calm-documents/v1/Documents",
        "testcases":  "/api/calm-testmanagement/v1/TestCases",
        "monitoring": "/api/calm-processmonitoring/v1/MonitoringEvents",
        "analytics":  "/api/calm-analytics/v1/",
        "hierarchy":  "/api/calm-processhierarchy/v1/ProcessHierarchyNodes",
    }
    system = (
        "You are an expert SAP Cloud ALM developer. Generate Python MCP tool functions.\n"
        "Use _calm_get(path, params) for GET, _calm_post(path, body) for POST.\n"
        "Each function: def func_name(ctx: Context, ...) -> str\n"
        "Return ONLY function definitions, no imports, no main block."
    )
    resp = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name).invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 4096,
                         "system": system,
                         "messages": [{"role": "user", "content":
                             f"Request: {prompt}\n\nAPIs:\n{json.dumps(calm_apis, indent=2)}\n\n"
                             f"Generate 4-8 focused tools. Use names like calm_list_monitoring_events."}]}),
        contentType="application/json", accept="application/json")
    return json.loads(resp["body"].read())["content"][0]["text"]

def _generate_sf_tools(prompt: str) -> str:
    sf_apis = {
        "User": "/odata/v2/User", "PerPerson": "/odata/v2/PerPerson",
        "EmpEmployment": "/odata/v2/EmpEmployment", "Position": "/odata/v2/Position",
        "FODepartment": "/odata/v2/FODepartment", "FOLocation": "/odata/v2/FOLocation",
        "JobRequisition": "/odata/v2/JobRequisition", "Candidate": "/odata/v2/Candidate",
        "LearningActivity": "/odata/v2/LearningActivity",
        "PerformanceReview": "/odata/v2/PerformanceReview",
        "CompensationEmployee": "/odata/v2/CompensationEmployee",
    }
    system = (
        "You are an expert SAP SuccessFactors developer. Generate Python MCP tool functions.\n"
        "Use _sf_get(path, params). Parse results from response.get('d',{}).get('results',[])\n"
        "Each function: def func_name(ctx: Context, top: int = 10, skip: int = 0, filter_expr: str = '') -> str\n"
        "Return ONLY function definitions, no imports, no main block."
    )
    resp = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name).invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 4096,
                         "system": system,
                         "messages": [{"role": "user", "content":
                             f"Request: {prompt}\n\nEntities:\n{json.dumps(sf_apis, indent=2)}\n\n"
                             f"Generate 4-8 focused tools. Use names like sf_list_employees."}]}),
        contentType="application/json", accept="application/json")
    return json.loads(resp["body"].read())["content"][0]["text"]


# ── Server templates ──────────────────────────────────────────────────────────
_S4_TEMPLATE = '''"""
{description} — Auto-generated AI Factory MCP Agent (S/4HANA).
"""
import os, json, logging, httpx
from mcp.server.fastmcp import FastMCP, Context
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("{agent_name}")
SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
mcp = FastMCP("{agent_name}", host="0.0.0.0", stateless_http=True)
def _get_token(ctx):
    try:
        val = ctx.request_context.request.headers.get("authorization","")
        if val.lower().startswith("bearer "): return val[7:]
    except: pass
    return os.environ.get("SAP_BEARER_TOKEN","")
def _sap_get(path, token, params=None):
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(f"{{SAP_BASE_URL}}{{path}}", headers={{"Authorization":f"Bearer {{token}}","Accept":"application/json"}}, params=params or {{}})
        r.raise_for_status(); return r.json()
{tools}
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
'''

_CALM_TEMPLATE = '''"""
{description} — Auto-generated AI Factory MCP Agent (Cloud ALM).
"""
import os, json, logging, httpx, time as _t
from mcp.server.fastmcp import FastMCP, Context
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("{agent_name}")
mcp = FastMCP("{agent_name}", host="0.0.0.0", stateless_http=True)
_tc: dict = {{"token": None, "exp": 0}}
def _get_calm_token():
    now = _t.time()
    if _tc["token"] and now < _tc["exp"]: return _tc["token"]
    tenant=os.environ.get("CALM_TENANT",""); region=os.environ.get("CALM_REGION","eu10")
    r = httpx.post(os.environ.get("CALM_TOKEN_URL",f"https://{{tenant}}.authentication.{{region}}.hana.ondemand.com/oauth/token"),
        data={{"grant_type":"client_credentials"}}, auth=(os.environ["CALM_CLIENT_ID"],os.environ["CALM_CLIENT_SECRET"]))
    r.raise_for_status(); d=r.json(); _tc["token"]=d["access_token"]; _tc["exp"]=now+d.get("expires_in",3600)-300; return _tc["token"]
def _calm_get(path, params=None):
    base=f"https://{{os.environ.get('CALM_TENANT','')}}.{{os.environ.get('CALM_REGION','eu10')}}.alm.cloud.sap"
    r=httpx.get(f"{{base}}{{path}}",params=params or {{}},headers={{"Authorization":f"Bearer {{_get_calm_token()}}","Accept":"application/json"}}); r.raise_for_status(); return r.json()
def _calm_post(path, body):
    base=f"https://{{os.environ.get('CALM_TENANT','')}}.{{os.environ.get('CALM_REGION','eu10')}}.alm.cloud.sap"
    r=httpx.post(f"{{base}}{{path}}",json=body,headers={{"Authorization":f"Bearer {{_get_calm_token()}}","Content-Type":"application/json"}}); r.raise_for_status(); return r.json() if r.content else {{}}
{tools}
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
'''

_SF_TEMPLATE = '''"""
{description} — Auto-generated AI Factory MCP Agent (SuccessFactors).
"""
import os, json, logging, httpx, time as _t
from mcp.server.fastmcp import FastMCP, Context
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("{agent_name}")
mcp = FastMCP("{agent_name}", host="0.0.0.0", stateless_http=True)
_tc: dict = {{"token": None, "exp": 0}}
def _get_sf_token():
    now=_t.time()
    if _tc["token"] and now < _tc["exp"]: return _tc["token"]
    dc=os.environ.get("SF_DC","4"); company_id=os.environ.get("SF_COMPANY_ID","")
    r=httpx.post(os.environ.get("SF_TOKEN_URL",f"https://api{{dc}}.successfactors.com/oauth/token"),
        data={{"grant_type":"client_credentials","company_id":company_id}},
        auth=(os.environ["SF_CLIENT_ID"],os.environ["SF_CLIENT_SECRET"]))
    r.raise_for_status(); d=r.json(); _tc["token"]=d["access_token"]; _tc["exp"]=now+d.get("expires_in",3600)-300; return _tc["token"]
def _sf_get(path, params=None):
    dc=os.environ.get("SF_DC","4")
    r=httpx.get(f"https://api{{dc}}.successfactors.com{{path}}",params={{**(params or {{}}), "$format":"json"}},
        headers={{"Authorization":f"Bearer {{_get_sf_token()}}","Accept":"application/json"}}); r.raise_for_status(); return r.json()
{tools}
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
'''


# ── Infrastructure provisioning ───────────────────────────────────────────────
def _ensure_infrastructure(region: str, ssm, cb, s3) -> tuple:
    """Idempotent: provision S3 + CodeBuild on first use, return (bucket, project)."""
    CODEBUILD_PROJECT = "sap-mcp-generator"
    account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    STAGING_BUCKET = f"sap-mcp-generator-{account_id}-{region}"
    try:
        bucket = ssm.get_parameter(Name="/sap_smart_agent/staging_bucket")["Parameter"]["Value"]
        project = ssm.get_parameter(Name="/sap_smart_agent/codebuild_project")["Parameter"]["Value"]
        return bucket, project
    except ssm.exceptions.ParameterNotFound:
        pass

    iam = boto3.client("iam", region_name=region)
    ROLE = "sap-mcp-generator-codebuild-role"
    trust = {"Version":"2012-10-17","Statement":[{"Effect":"Allow",
        "Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]}
    try:
        role_arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]

    iam.put_role_policy(RoleName=ROLE, PolicyName="policy", PolicyDocument=json.dumps({
        "Version":"2012-10-17","Statement":[
            {"Effect":"Allow","Action":["logs:*"],"Resource":f"arn:aws:logs:{region}:{account_id}:log-group:/aws/codebuild/{CODEBUILD_PROJECT}*"},
            {"Effect":"Allow","Action":["s3:*"],"Resource":[f"arn:aws:s3:::{STAGING_BUCKET}",f"arn:aws:s3:::{STAGING_BUCKET}/*"]},
            {"Effect":"Allow","Action":["ssm:GetParameter","ssm:PutParameter"],"Resource":f"arn:aws:ssm:{region}:{account_id}:parameter/sap_*"},
            {"Effect":"Allow","Action":["ecr:*","bedrock-agentcore:*","iam:CreateRole","iam:AttachRolePolicy","iam:PassRole","iam:GetRole","iam:PutRolePolicy"],"Resource":"*"},
        ]}))

    try:
        if region == "us-east-1": s3.create_bucket(Bucket=STAGING_BUCKET)
        else: s3.create_bucket(Bucket=STAGING_BUCKET, CreateBucketConfiguration={"LocationConstraint": region})
    except Exception: pass

    import time as _time; _time.sleep(10)

    buildspec = """version: 0.2
env:
  parameter-store:
    OKTA_CLIENT_ID: "/sap_smart_agent/okta_client_id"
    OKTA_DOMAIN: "/sap_smart_agent/okta_domain"
phases:
  install:
    runtime-versions:
      python: 3.11
    commands:
      - pip install bedrock-agentcore-starter-toolkit boto3 --quiet
  build:
    commands:
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/server.py ./server.py
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/requirements.txt ./requirements.txt
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/meta.json ./meta.json
      - |
        python - <<'EOF'
        import os,json,time,boto3
        from bedrock_agentcore_starter_toolkit import Runtime
        from boto3.session import Session
        agent_name=json.load(open("meta.json"))["agent_name"]
        region=Session().region_name
        runtime=Runtime()
        auth={"customJWTAuthorizer":{"allowedClients":[os.environ["OKTA_CLIENT_ID"]],
            "discoveryUrl":f"https://{os.environ['OKTA_DOMAIN']}/oauth2/default/.well-known/openid-configuration"}}
        runtime.configure(entrypoint="server.py",auto_create_execution_role=True,auto_create_ecr=True,
            requirements_file="requirements.txt",region=region,authorizer_configuration=auth,
            protocol="MCP",agent_name=agent_name)
        result=runtime.launch(auto_update_on_conflict=True)
        while True:
            status=runtime.status().endpoint["status"]
            print(f"Status: {status}")
            if status in ["READY","CREATE_FAILED","UPDATE_FAILED"]: break
            time.sleep(15)
        if status=="READY":
            boto3.client("ssm",region_name=region).put_parameter(
                Name=f"/sap_generated/{agent_name}/agent_arn",Value=result.agent_arn,Type="String",Overwrite=True)
            print(f"DEPLOY_SUCCESS:{result.agent_arn}")
        else: exit(1)
        EOF
"""
    try:
        cb.create_project(name=CODEBUILD_PROJECT, description="SAP MCP generator",
            source={"type":"NO_SOURCE","buildspec":buildspec},
            artifacts={"type":"NO_ARTIFACTS"},
            environment={"type":"LINUX_CONTAINER","image":"aws/codebuild/standard:7.0",
                         "computeType":"BUILD_GENERAL1_SMALL","privilegedMode":True},
            serviceRole=role_arn, timeoutInMinutes=30)
    except cb.exceptions.ResourceAlreadyExistsException: pass

    okta_domain = os.environ.get("OKTA_DOMAIN","trial-1053860.okta.com")
    okta_client_id = os.environ.get("OKTA_CLIENT_ID","0oa10vth79kZAuXGt698")
    for k,v in {"/sap_smart_agent/staging_bucket":STAGING_BUCKET,
                "/sap_smart_agent/codebuild_project":CODEBUILD_PROJECT,
                "/sap_smart_agent/okta_domain":okta_domain,
                "/sap_smart_agent/okta_client_id":okta_client_id}.items():
        ssm.put_parameter(Name=k, Value=v, Type="String", Overwrite=True)
    return STAGING_BUCKET, CODEBUILD_PROJECT


# ── Post-deploy: write bridge + update mcp.json ───────────────────────────────
def _write_agent_bridge_and_mcp_config(agent_name: str, region: str):
    """Write the correct kiro_bridge.py to the agent folder and add entry to mcp.json."""
    workspace_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    agent_dir = os.path.join(workspace_root, f"sap-{agent_name.replace('_', '-')}-agent")
    os.makedirs(agent_dir, exist_ok=True)

    # Use the working bridge template (MCP streamable HTTP, no ROUTER_TOOLS filter)
    working_bridge = os.path.join(workspace_root, "sap-smart-agent", "kiro_bridge.py")
    with open(working_bridge) as f:
        bridge_src = f.read()

    # Strip the ROUTER_TOOLS filter — generated agents expose all their tools
    bridge_src = bridge_src.replace(
        '"""Kiro stdio bridge for AI Factory MCP Server — Okta 3LO → AgentCore or local server.\nStrips outputSchema from all tools to prevent Kiro validation errors.\n"""',
        f'"""Kiro stdio bridge for {agent_name} — Okta 3LO → AgentCore.\nAuto-generated by generator_agent. Uses MCP streamable HTTP (same pattern as ai-factory).\n"""'
    )
    # Remove ROUTER_TOOLS filter block
    bridge_src = bridge_src.replace(
        '\nROUTER_TOOLS = {\n    "adt_agent_tool", "odata_agent_tool", "calm_agent_tool",\n    "sf_agent_tool", "generator_agent_tool"\n}\n',
        '\n'
    )
    bridge_src = bridge_src.replace(
        'server = Server("ai-factory-bridge")',
        f'server = Server("{agent_name}-bridge")'
    )
    # Remove the ROUTER_TOOLS filtering logic in list_tools
    bridge_src = bridge_src.replace(
        '            tools = result.tools\n            # Filter to router tools if this is the ai-factory server\n            filtered = [t for t in tools if t.name in ROUTER_TOOLS]\n            if filtered:\n                tools = filtered\n            return _strip_output_schema(tools)',
        '            return _strip_output_schema(result.tools)'
    )

    bridge_path = os.path.join(agent_dir, "kiro_bridge.py")
    with open(bridge_path, "w") as f:
        f.write(bridge_src)
    logger.info(f"Written bridge: {bridge_path}")

    # Update mcp.json — find next available callback port
    mcp_json_path = os.path.join(workspace_root, ".kiro", "settings", "mcp.json")
    try:
        with open(mcp_json_path) as f:
            mcp_config = json.load(f)
    except Exception:
        mcp_config = {"mcpServers": {}}

    servers = mcp_config.get("mcpServers", {})

    # Find next free port (start at 8090, skip used ones)
    used_ports = set()
    for s in servers.values():
        uri = s.get("env", {}).get("OKTA_REDIRECT_URI", "")
        if uri:
            try: used_ports.add(int(uri.split(":")[-1].split("/")[0]))
            except: pass
    port = 8090
    while port in used_ports:
        port += 1

    server_key = agent_name.replace("_", "-")
    if server_key not in servers:
        servers[server_key] = {
            "command": "python",
            "args": [f"sap-{server_key}-agent/kiro_bridge.py"],
            "env": {
                "OKTA_DOMAIN": os.environ.get("OKTA_DOMAIN", "trial-1053860.okta.com"),
                "OKTA_AUTH_SERVER": "default",
                "OKTA_CLIENT_ID": os.environ.get("OKTA_CLIENT_ID", ""),
                "OKTA_CLIENT_SECRET": os.environ.get("OKTA_CLIENT_SECRET", ""),
                "OKTA_SCOPES": "openid email",
                "OKTA_REDIRECT_URI": f"http://localhost:{port}/callback",
                "AGENTCORE_ARN_SSM_PARAM": f"/sap_generated/{agent_name}/agent_arn"
            },
            "disabled": False,
            "autoApprove": []
        }
        mcp_config["mcpServers"] = servers
        with open(mcp_json_path, "w") as f:
            json.dump(mcp_config, f, indent=2)
        logger.info(f"Added {server_key} to mcp.json on port {port}")
    else:
        logger.info(f"{server_key} already in mcp.json — skipping")


# ── Main tool ─────────────────────────────────────────────────────────────────
@mcp.tool()
def generate_and_deploy_mcp_server(ctx: Context, prompt: str, agent_name: str) -> str:
    """Generate a new focused SAP MCP agent and deploy it to AWS AgentCore Runtime via CodeBuild.

    Part of the AI Factory MCP Server. Use this when a user asks to create, build, or deploy
    a new dedicated agent for a specific SAP domain or use case.

    Automatically detects the target domain from the prompt:
    - 'calm' — SAP Cloud ALM / Rise / Cloud ERP (projects, tasks, monitoring, analytics)
    - 'sf'   — SAP SuccessFactors HCM (employees, recruiting, learning, performance)
    - 's4'   — SAP S/4HANA OData (default — auto-discovers relevant OData services from catalog)

    The agent is generated using Claude (claude-sonnet-4-6), packaged, uploaded to S3,
    and deployed to AgentCore via CodeBuild (~10-15 min). Once deployed, the ARN is stored
    in SSM at /sap_generated/{agent_name}/agent_arn and can be wired into the AI Factory.

    Args:
        prompt:     Natural language description of what the agent should do.
                    e.g. 'Cloud ALM monitoring agent that tracks process exceptions and alerts'
                    e.g. 'SuccessFactors agent for employee headcount and performance data'
                    e.g. 'Plant maintenance agent for orders, notifications and equipment'
        agent_name: Short lowercase underscore identifier for the agent.
                    e.g. 'calm_monitoring_agent', 'sf_hr_agent', 'pm_maintenance_agent'

    Returns JSON with deployment status, domain, services used, and CodeBuild build ID.
    """
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})

    try:
        region = boto3.session.Session().region_name
        ssm = boto3.client("ssm", region_name=region)
        s3  = boto3.client("s3",  region_name=region)
        cb  = boto3.client("codebuild", region_name=region)

        staging_bucket, codebuild_project = _ensure_infrastructure(region, ssm, cb, s3)
        domain = _detect_domain(prompt)
        logger.info(f"Domain: {domain} | Agent: {agent_name} | Prompt: {prompt}")

        if domain == "calm":
            tools_code   = _generate_calm_tools(prompt)
            server_code  = _CALM_TEMPLATE.format(
                description=f"SAP Cloud ALM agent: {prompt}", agent_name=agent_name, tools=tools_code)
            services_used = ["calm-projects", "calm-tasks", "calm-monitoring", "calm-analytics"]

        elif domain == "sf":
            tools_code   = _generate_sf_tools(prompt)
            server_code  = _SF_TEMPLATE.format(
                description=f"SAP SuccessFactors agent: {prompt}", agent_name=agent_name, tools=tools_code)
            services_used = ["sf-odata-v2"]

        else:  # s4
            stop = {"with","that","this","from","have","will","should","tools","tool",
                    "details","including","information","related","help","about","also",
                    "their","them","these","those","into","like","such","some","other",
                    "more","most","very","just","only","both","each","every"}
            keywords = list(dict.fromkeys(
                w.strip(",.;:!?") for w in prompt.lower().split()
                if len(w.strip(",.;:!?")) > 3 and w.strip(",.;:!?") not in stop))
            matched = _discover_relevant_services(token, keywords)
            if not matched:
                return json.dumps({"error": f"No S/4 services found for: {prompt}"})
            svcs_with_entities = []
            for svc in matched[:5]:
                path = f"/sap/opu/odata/sap/{svc['Title']}"
                entities = _get_entities_for_service(path, token)
                if entities:
                    svcs_with_entities.append({"service_path": path,
                                               "title": svc["Title"], "entities": entities})
            if not svcs_with_entities:
                return json.dumps({"error": "Could not retrieve metadata for matched services."})
            tools_code  = _generate_s4_tools(prompt, svcs_with_entities, agent_name)
            server_code = _S4_TEMPLATE.format(
                description=f"SAP S/4HANA agent: {prompt}", agent_name=agent_name, tools=tools_code)
            services_used = [s["service_path"] for s in svcs_with_entities]

        # Upload to S3 + trigger CodeBuild
        build_id = str(uuid.uuid4())[:8]
        prefix   = f"generated/{build_id}"
        requirements = "fastmcp\nhttpx\nbedrock-agentcore-starter-toolkit\nboto3\n"
        meta = {"agent_name": agent_name, "prompt": prompt, "domain": domain, "services": services_used}

        for key, content in [(f"{prefix}/server.py", server_code),
                              (f"{prefix}/requirements.txt", requirements),
                              (f"{prefix}/meta.json", json.dumps(meta))]:
            s3.put_object(Bucket=staging_bucket, Key=key, Body=content.encode())

        build_resp = cb.start_build(
            projectName=codebuild_project,
            environmentVariablesOverride=[
                {"name": "BUILD_ID",        "value": build_id,        "type": "PLAINTEXT"},
                {"name": "STAGING_BUCKET",  "value": staging_bucket,  "type": "PLAINTEXT"},
            ])
        cb_build_id = build_resp["build"]["id"]

        # Write the correct kiro_bridge.py and update mcp.json for this new agent
        _write_agent_bridge_and_mcp_config(agent_name, region)

        return json.dumps({
            "status":           "deploying",
            "agent_name":       agent_name,
            "domain":           domain,
            "services_used":    services_used,
            "codebuild_build_id": cb_build_id,
            "ssm_arn_key":      f"/sap_generated/{agent_name}/agent_arn",
            "message": (
                f"CodeBuild started for '{agent_name}' ({domain} domain). "
                f"~10-15 min to deploy. "
                f"kiro_bridge.py written and mcp.json updated — restart the MCP server after deploy. "
                f"Track: aws codebuild batch-get-builds --ids '{cb_build_id}'"
            )
        }, indent=2)

    except Exception as e:
        logger.error(f"generate_and_deploy_mcp_server failed: {e}")
        return json.dumps({"error": str(e)})


# ── Strands wrapper ───────────────────────────────────────────────────────────
def create_generator_strands_agent() -> Agent:
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    client = MCPClient(lambda: streamablehttp_client("http://localhost:8105/mcp"))
    with client:
        return Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=32000),
            tools=client.tools,
            system_prompt=(
                "You are the Generator Agent within the AI Factory MCP Server.\n\n"
                "Your role is to create and deploy new focused SAP MCP agents to AWS AgentCore. "
                "When a user asks to build, create, or deploy a new agent for a specific SAP domain, "
                "use generate_and_deploy_mcp_server.\n\n"
                "Domain detection is automatic:\n"
                "- 'calm' — SAP Cloud ALM (Rise/Cloud ERP: projects, monitoring, analytics)\n"
                "- 'sf'   — SAP SuccessFactors (HR: employees, recruiting, learning, performance)\n"
                "- 's4'   — SAP S/4HANA OData (default: discovers services from the catalog)\n\n"
                "Before deploying, always confirm:\n"
                "1. The agent_name (must be lowercase with underscores, e.g. calm_monitoring_agent)\n"
                "2. The domain detected from the prompt\n"
                "3. What the agent will do\n\n"
                "Deployment takes ~10-15 minutes via CodeBuild. "
                "The agent ARN is stored in SSM at /sap_generated/{agent_name}/agent_arn once complete."
            )
        )


if __name__ == "__main__":
    logger.info("=== SAP Generator Agent starting on port 8105 ===")
    mcp.run(transport="streamable-http")
