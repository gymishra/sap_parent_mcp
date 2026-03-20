"""
SAP Smart MCP Server — AI-powered SAP OData + ADT agent.
Token is extracted from the incoming Authorization header — never passed as a tool parameter.
"""
import os, json, logging, httpx, uuid, boto3
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sap_smart")

SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
CATALOG_URL = "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"

mcp = FastMCP("SAP Smart Agent", host="0.0.0.0", stateless_http=True)

_catalog_cache = {}
_metadata_cache = {}


def _get_token(ctx: Context, bearer_token: str = "") -> str:
    """Get Bearer token — tries request context header first, then explicit param, then env var."""
    # 1. Try incoming HTTP Authorization header
    try:
        auth = ctx.request_context.request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
            if token:
                logger.info("Token extracted from request header")
                return token
    except Exception as e:
        logger.warning(f"Could not extract token from context: {e}")
    # 2. Explicit parameter (injected by bridge for AgentCore deployments)
    if bearer_token:
        logger.info("Token extracted from bearer_token parameter")
        return bearer_token
    # 3. Env var fallback
    env_token = os.environ.get("SAP_BEARER_TOKEN", "")
    if env_token:
        logger.info("Token extracted from SAP_BEARER_TOKEN env var")
    else:
        logger.error("No token found in header, parameter, or env var!")
    return env_token


def _sap_get(path, token, params=None):
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                  params=params or {})
        r.raise_for_status()
        return r.json()


def _sap_get_xml(path, token):
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})
        r.raise_for_status()
        return r.text


# ── Tool 0: ADT API ───────────────────────────────────────────────────────────

@mcp.tool()
def call_adt_api(ctx: Context, adt_path: str, method: str = "GET", query_params: str = "",
                 body: str = "", content_type: str = "", accept: str = "",
                 bearer_token: str = "") -> str:
    """Call SAP ADT REST API. Single tool for all ABAP development operations.
    Supports GET, POST, PUT, DELETE with automatic CSRF token handling.
    Args:
        adt_path: ADT REST path (e.g. '/sap/bc/adt/programs/programs/SAPMV45A/source/main')
        method: HTTP method - GET, POST, PUT, DELETE (default GET)
        query_params: URL query string (e.g. 'operation=quickSearch&query=Z*&maxResults=10')
        body: Request body for POST/PUT (source code, XML payload, SQL query etc.)
        content_type: Content-Type header override
        accept: Accept header override (e.g. 'application/vnd.sap.as+xml;charset=UTF-8;dataname=com.sap.adt.lock.result')
    """
    token = _get_token(ctx, bearer_token)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        params = {}
        if query_params:
            for pair in query_params.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = v

        url = f"{SAP_BASE_URL}{adt_path}"
        http_method = method.upper()

        with httpx.Client(verify=False, timeout=60.0) as c:
            base_headers = {"Authorization": f"Bearer {token}"}

            if http_method == "GET":
                default_accept = "text/plain" if "/source/main" in adt_path else "application/xml"
                r = c.get(url, headers={**base_headers, "Accept": accept or default_accept}, params=params)
                if r.status_code == 405:
                    csrf_resp = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                                     headers={**base_headers, "x-csrf-token": "Fetch", "Accept": "*/*"})
                    csrf = csrf_resp.headers.get("x-csrf-token", "")
                    r = c.post(url, headers={**base_headers, "Accept": accept or "application/xml",
                               "x-csrf-token": csrf, "Content-Type": "application/xml"}, params=params, content=body or "")
            else:
                csrf_resp = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                                 headers={**base_headers, "x-csrf-token": "Fetch", "Accept": "*/*"})
                csrf = csrf_resp.headers.get("x-csrf-token", "")
                ct = content_type or ("text/plain" if "/source/main" in adt_path and http_method == "PUT" else "application/xml")
                req_headers = {**base_headers, "x-csrf-token": csrf, "Content-Type": ct, "Accept": accept or "application/xml"}
                if http_method == "POST":
                    r = c.post(url, headers=req_headers, params=params, content=body or "")
                elif http_method == "PUT":
                    r = c.put(url, headers=req_headers, params=params, content=body or "")
                elif http_method == "DELETE":
                    r = c.delete(url, headers=req_headers, params=params)
                else:
                    return json.dumps({"error": f"Unsupported method: {method}"})

            r.raise_for_status()
            resp_text = r.text

        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(resp_text)
            results = []
            for child in root:
                item = dict(child.attrib)
                for sub in child:
                    tag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                    item[tag] = sub.text or dict(sub.attrib)
                results.append(item)
            if results:
                return json.dumps({"count": len(results), "results": results}, indent=2)
            if root.attrib:
                return json.dumps({"result": dict(root.attrib)}, indent=2)
        except ET.ParseError:
            pass
        if len(resp_text) > 5000:
            return json.dumps({"source": resp_text[:5000], "truncated": True, "total_length": len(resp_text)})
        return json.dumps({"response": resp_text}) if resp_text else json.dumps({"status": "success", "http_status": r.status_code})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 1: Discover SAP Services ─────────────────────────────────────────────

@mcp.tool()
def discover_sap_services(ctx: Context, bearer_token: str = "") -> str:
    """Discover all available OData services from SAP catalog."""
    global _catalog_cache
    token = _get_token(ctx, bearer_token)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        data = _sap_get(CATALOG_URL, token, {"$format": "json"})
        services = []
        for svc in data.get("d", {}).get("results", []):
            entry = {
                "Title": svc.get("Title", ""),
                "TechnicalServiceName": svc.get("TechnicalServiceName", ""),
                "ServiceUrl": svc.get("ServiceUrl", ""),
                "Description": svc.get("Description", ""),
                "TechnicalServiceVersion": svc.get("TechnicalServiceVersion", ""),
            }
            services.append(entry)
            _catalog_cache[entry["TechnicalServiceName"]] = entry
        logger.info(f"Discovered {len(services)} SAP services")
        return json.dumps({"service_count": len(services), "services": services}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 2: Get Service Metadata ──────────────────────────────────────────────

@mcp.tool()
def get_service_metadata(ctx: Context, service_path: str, bearer_token: str = "") -> str:
    """Get entity types and properties for a SAP OData service.
    Args:
        service_path: OData service path (e.g. '/sap/opu/odata/sap/API_SALES_ORDER_SRV')
    """
    global _metadata_cache
    token = _get_token(ctx, bearer_token)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        metadata_xml = _sap_get_xml(f"{service_path}/$metadata", token)
        entities = []
        import xml.etree.ElementTree as ET
        root = ET.fromstring(metadata_xml)
        for ns_uri in ["http://schemas.microsoft.com/ado/2008/09/edm",
                       "http://schemas.microsoft.com/ado/2009/11/edm"]:
            for entity_type in root.iter(f"{{{ns_uri}}}EntityType"):
                name = entity_type.get("Name", "")
                props = [{"Name": p.get("Name"), "Type": p.get("Type")}
                         for p in entity_type.iter(f"{{{ns_uri}}}Property")]
                entities.append({"EntityType": name, "Properties": props[:20]})
        _metadata_cache[service_path] = entities
        logger.info(f"Metadata for {service_path}: {len(entities)} entity types")
        return json.dumps({"service": service_path, "entity_types": entities}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 3: Query OData entity set ────────────────────────────────────────────

@mcp.tool()
def query_sap_odata(ctx: Context, service_path: str, entity_set: str, top: int = 10,
                    skip: int = 0, filter_expr: str = "", select_fields: str = "",
                    bearer_token: str = "") -> str:
    """Query any SAP OData entity set.
    Args:
        service_path: OData service path (e.g. '/sap/opu/odata/sap/API_SALES_ORDER_SRV')
        entity_set: Entity set name (e.g. 'A_SalesOrder')
        top: Max records to return (default 10)
        skip: Records to skip for pagination (default 0)
        filter_expr: OData $filter expression (e.g. "SalesOrder eq '4'")
        select_fields: Comma-separated fields to return
    """
    token = _get_token(ctx, bearer_token)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        params = {"$format": "json", "$top": str(top), "$skip": str(skip)}
        if filter_expr:   params["$filter"] = filter_expr
        if select_fields: params["$select"] = select_fields
        path = f"{service_path}/{entity_set}"
        data = _sap_get(path, token, params)
        results = data.get("d", {}).get("results", [])
        if not results and "d" in data:
            d = data["d"]
            if isinstance(d, dict) and "results" not in d:
                results = [d]
        logger.info(f"Query {path}: {len(results)} results")
        return json.dumps({"count": len(results), "results": results}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 4: Get single entity by key ──────────────────────────────────────────

@mcp.tool()
def get_sap_entity(ctx: Context, service_path: str, entity_set: str, key: str,
                   bearer_token: str = "") -> str:
    """Get a single SAP entity by its key value.
    Args:
        service_path: OData service path
        entity_set: Entity set name
        key: Entity key value (e.g. '0000000004')
    """
    token = _get_token(ctx, bearer_token)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        path = f"{service_path}/{entity_set}('{key}')"
        data = _sap_get(path, token, {"$format": "json"})
        return json.dumps(data.get("d", {}), indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 5: Update SAP entity (PATCH) ─────────────────────────────────────────

@mcp.tool()
def update_sap_entity(ctx: Context, service_path: str, entity_set: str,
                      key: str, payload: str, bearer_token: str = "") -> str:
    """Update a SAP entity via PATCH. Handles CSRF token and ETag automatically.
    Args:
        service_path: OData service path
        entity_set: Entity set name
        key: Entity key expression (e.g. "SalesOrder='2',SalesOrderItem='10'")
        payload: JSON string with fields to update (e.g. '{"RequestedQuantity": "5"}')
    """
    token = _get_token(ctx, bearer_token)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        update_data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON payload: {e}"})

    entity_url = f"{SAP_BASE_URL}{service_path}/{entity_set}({key})"
    try:
        with httpx.Client(verify=False, timeout=30.0) as c:
            fetch_resp = c.get(entity_url, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json", "x-csrf-token": "Fetch"
            }, params={"$format": "json"})
            fetch_resp.raise_for_status()
            csrf_token = fetch_resp.headers.get("x-csrf-token", "")
            etag = fetch_resp.headers.get("etag", "*")
            if not csrf_token:
                return json.dumps({"error": "SAP did not return a CSRF token"})

            patch_resp = c.patch(entity_url, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                "Accept": "application/json", "x-csrf-token": csrf_token, "If-Match": etag,
            }, content=json.dumps(update_data))

            if patch_resp.status_code in (200, 204):
                verify_resp = c.get(entity_url, headers={
                    "Authorization": f"Bearer {token}", "Accept": "application/json"
                }, params={"$format": "json"})
                if verify_resp.status_code == 200:
                    return json.dumps({"status": "success", "updated_entity": verify_resp.json().get("d", {})}, indent=2)
                return json.dumps({"status": "success", "message": f"Updated {entity_url}"})
            else:
                return json.dumps({"error": f"PATCH returned {patch_resp.status_code}", "details": patch_resp.text[:500]})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 6: Create SAP entity (POST) ──────────────────────────────────────────

@mcp.tool()
def create_sap_entity(ctx: Context, service_path: str, entity_set: str, payload: str,
                      bearer_token: str = "") -> str:
    """Create a new SAP entity via POST. Handles CSRF token automatically.
    Args:
        service_path: OData service path
        entity_set: Entity set name
        payload: JSON string with entity data to create
    """
    token = _get_token(ctx, bearer_token)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        create_data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON payload: {e}"})

    entity_url = f"{SAP_BASE_URL}{service_path}/{entity_set}"
    try:
        with httpx.Client(verify=False, timeout=30.0) as c:
            fetch_resp = c.get(f"{SAP_BASE_URL}{service_path}/", headers={
                "Authorization": f"Bearer {token}", "x-csrf-token": "Fetch"
            })
            csrf_token = fetch_resp.headers.get("x-csrf-token", "")
            if not csrf_token:
                return json.dumps({"error": "SAP did not return a CSRF token"})

            post_resp = c.post(entity_url, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                "Accept": "application/json", "x-csrf-token": csrf_token,
            }, content=json.dumps(create_data))

            if post_resp.status_code in (200, 201):
                return json.dumps({"status": "created", "entity": post_resp.json().get("d", {})}, indent=2)
            else:
                return json.dumps({"error": f"POST returned {post_resp.status_code}", "details": post_resp.text[:500]})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("sap://catalog")
def get_cached_catalog() -> str:
    """Returns the cached SAP service catalog."""
    if not _catalog_cache:
        return json.dumps({"message": "Catalog not loaded. Call discover_sap_services first."})
    return json.dumps(list(_catalog_cache.values()), indent=2)


@mcp.resource("sap://metadata/{service_name}")
def get_cached_metadata(service_name: str) -> str:
    """Returns cached metadata for a specific service."""
    for key, val in _metadata_cache.items():
        if service_name in key:
            return json.dumps(val, indent=2)
    return json.dumps({"message": f"No cached metadata for {service_name}."})


# ── Tool 7: Generate & Deploy a new MCP Server ───────────────────────────────

_SERVER_TEMPLATE = '''"""
{description}
Auto-generated by SAP Smart Agent.
"""
import os, json, logging, httpx
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("{agent_name}")

SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
mcp = FastMCP("{agent_name}", host="0.0.0.0", stateless_http=True)

def _get_token(ctx: Context, bearer_token: str = "") -> str:
    try:
        auth = ctx.request_context.request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            t = auth[7:]
            if t: return t
    except: pass
    if bearer_token: return bearer_token
    return os.environ.get("SAP_BEARER_TOKEN", "")

def _sap_get(path, token, params=None):
    url = f"{{SAP_BASE_URL}}{{path}}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={{"Authorization": f"Bearer {{token}}", "Accept": "application/json"}}, params=params or {{}})
        r.raise_for_status()
        return r.json()

{tools}

if __name__ == "__main__":
    logger.info("=== {agent_name} Starting ===")
    mcp.run(transport="streamable-http")
'''

_BUILDSPEC_INLINE = """version: 0.2
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
  pre_build:
    commands:
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/server.py ./sap_generated_mcp_server.py
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/requirements.txt ./requirements_generated.txt
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/meta.json ./meta.json
      - AGENT_NAME=$(python -c "import json; print(json.load(open('meta.json'))['agent_name'])")
  build:
    commands:
      - |
        python - <<'EOF'
        import os, json, time, boto3
        from bedrock_agentcore_starter_toolkit import Runtime
        from boto3.session import Session
        agent_name = json.load(open("meta.json"))["agent_name"]
        okta_domain = os.environ["OKTA_DOMAIN"]
        okta_client_id = os.environ["OKTA_CLIENT_ID"]
        region = Session().region_name
        runtime = Runtime()
        auth_config = {"customJWTAuthorizer": {"allowedClients": [okta_client_id],
            "discoveryUrl": f"https://{okta_domain}/oauth2/default/.well-known/openid-configuration"}}
        runtime.configure(entrypoint="sap_generated_mcp_server.py", auto_create_execution_role=True,
            auto_create_ecr=True, requirements_file="requirements_generated.txt", region=region,
            authorizer_configuration=auth_config, protocol="MCP", agent_name=agent_name)
        result = runtime.launch(auto_update_on_conflict=True)
        while True:
            status = runtime.status().endpoint["status"]
            print(f"Status: {status}")
            if status in ["READY", "CREATE_FAILED", "UPDATE_FAILED"]: break
            time.sleep(15)
        if status == "READY":
            ssm = boto3.client("ssm", region_name=region)
            ssm.put_parameter(Name=f"/sap_generated/{agent_name}/agent_arn",
                Value=result.agent_arn, Type="String", Overwrite=True)
            print(f"DEPLOY_SUCCESS:{result.agent_arn}")
        else:
            exit(1)
        EOF
"""

_TOOL_TEMPLATE = '''
def {func_name}(ctx: Context, top: int = 10, skip: int = 0, filter_expr: str = "", bearer_token: str = "") -> str:
    """{docstring}"""
    token = _get_token(ctx, bearer_token)
    if not token:
        return json.dumps({{"error": "No bearer token"}})
    try:
        params = {{"$format": "json", "$top": str(top), "$skip": str(skip)}}
        if filter_expr: params["$filter"] = filter_expr
        data = _sap_get("{service_path}/{entity_set}", token, params)
        results = data.get("d", {{}}).get("results", [])
        return json.dumps({{"count": len(results), "results": results}}, indent=2)
    except Exception as e:
        return json.dumps({{"error": str(e)}})
'''


def _discover_relevant_services(token: str, domain_keywords: list) -> list:
    """Query SAP catalog and filter services matching domain keywords."""
    try:
        data = _sap_get(CATALOG_URL, token, {"$format": "json"})
        services = data.get("d", {}).get("results", [])
        logger.info(f"Catalog returned {len(services)} total services, filtering with keywords: {domain_keywords}")
        matched = []
        for svc in services:
            title = (svc.get("Title", "") + svc.get("TechnicalServiceName", "") +
                     svc.get("Description", "")).lower()
            if any(kw.lower() in title for kw in domain_keywords):
                matched.append({
                    "Title": svc.get("Title", ""),
                    "TechnicalServiceName": svc.get("TechnicalServiceName", ""),
                    "ServiceUrl": svc.get("ServiceUrl", ""),
                })
        # Prioritize standard SAP API services over custom Z-services
        api_services = [s for s in matched if s["Title"].startswith("API_")]
        other_std = [s for s in matched if not s["TechnicalServiceName"].startswith("Z")
                     and not s["Title"].startswith("API_")]
        z_services = [s for s in matched if s["TechnicalServiceName"].startswith("Z")
                      and not s["Title"].startswith("API_")]
        prioritized = api_services + other_std + z_services
        logger.info(f"Matched {len(matched)} services ({len(api_services)} API_, {len(other_std)} other std, {len(z_services)} Z-custom)")
        logger.info(f"Top 10: {[s['TechnicalServiceName'] for s in prioritized[:10]]}")
        return prioritized[:10]
    except Exception as e:
        logger.error(f"Service discovery failed: {e}")
        return []


def _get_entities_for_service(service_path: str, token: str) -> list:
    """Get top entity types for a service."""
    try:
        import xml.etree.ElementTree as ET
        logger.info(f"Fetching metadata for: {service_path}")
        xml_text = _sap_get_xml(f"{service_path}/$metadata", token)
        logger.info(f"Metadata response length: {len(xml_text)} chars")
        root = ET.fromstring(xml_text)
        entities = []
        for ns_uri in ["http://schemas.microsoft.com/ado/2008/09/edm",
                       "http://schemas.microsoft.com/ado/2009/11/edm",
                       "http://docs.oasis-open.org/odata/ns/edm"]:
            for et in root.iter(f"{{{ns_uri}}}EntityType"):
                name = et.get("Name", "")
                if name and not name.startswith("I_") and not name.startswith("SAP__"):
                    entities.append(name)
        logger.info(f"Found {len(entities)} entities for {service_path}")
        return entities[:5]
    except Exception as e:
        logger.error(f"_get_entities_for_service failed for {service_path}: {e}")
        return []


def _generate_server_code_with_bedrock(prompt: str, services_with_entities: list,
                                        agent_name: str) -> str:
    """Use Claude via Bedrock to generate the MCP server tool functions."""
    bedrock = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name)

    services_summary = json.dumps(services_with_entities, indent=2)
    system = (
        "You are an expert SAP developer. Generate Python MCP tool functions for a FastMCP server. "
        "Each tool must query one SAP OData entity set. "
        "\n\nCRITICAL: The entity names in the metadata are ENTITY TYPES (e.g. A_SalesOrderType, "
        "A_SalesOrderHeaderPartnerType). In the OData URL path you must use the ENTITY SET name, "
        "which is the type name WITHOUT the 'Type' suffix. Examples:\n"
        "  - A_SalesOrderType -> use A_SalesOrder in the URL\n"
        "  - A_SalesOrderHeaderPartnerType -> use A_SalesOrderHeaderPartner in the URL\n"
        "  - A_MaintenanceOrderType -> use A_MaintenanceOrder in the URL\n"
        "NEVER use the 'Type' suffix in OData URL paths.\n\n"
        "Use ONLY this exact function signature pattern:\n"
        "@mcp.tool()\n"
        "def func_name(ctx: Context, top: int = 10, skip: int = 0, filter_expr: str = \"\", bearer_token: str = \"\") -> str:\n"
        "    \"\"\"docstring\"\"\"\n"
        "    token = _get_token(ctx, bearer_token)\n"
        "    if not token: return json.dumps({\"error\": \"No bearer token\"})\n"
        "    try:\n"
        "        params = {\"$format\": \"json\", \"$top\": str(top), \"$skip\": str(skip)}\n"
        "        if filter_expr: params[\"$filter\"] = filter_expr\n"
        "        data = _sap_get(\"<service_path>/<entity_set_without_Type_suffix>\", token, params)\n"
        "        results = data.get(\"d\", {}).get(\"results\", [])\n"
        "        return json.dumps({\"count\": len(results), \"results\": results}, indent=2)\n"
        "    except Exception as e:\n"
        "        return json.dumps({\"error\": str(e)})\n\n"
        "Return ONLY the tool function definitions, no imports, no main block."
    )
    user = (
        f"User request: {prompt}\n\n"
        f"Available SAP services and entities:\n{services_summary}\n\n"
        f"Generate 3-7 MCP tool functions covering the most useful operations for this domain. "
        f"Use snake_case function names that describe what the tool does (e.g. get_maintenance_orders, list_notifications)."
    )

    response = bedrock.invoke_model(
        modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": "user", "content": user}]
        }),
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response["body"].read())
    return result["content"][0]["text"]


def _ensure_generator_infrastructure(region, ssm, cb, s3) -> tuple:
    """Auto-provision S3 bucket + CodeBuild project on first use. Idempotent."""
    CODEBUILD_PROJECT = "sap-mcp-generator"
    account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    STAGING_BUCKET = f"sap-mcp-generator-{account_id}-{region}"

    # Check if already set up
    try:
        staging_bucket = ssm.get_parameter(Name="/sap_smart_agent/staging_bucket")["Parameter"]["Value"]
        codebuild_project = ssm.get_parameter(Name="/sap_smart_agent/codebuild_project")["Parameter"]["Value"]
        logger.info("Generator infrastructure already provisioned")
        return staging_bucket, codebuild_project
    except ssm.exceptions.ParameterNotFound:
        logger.info("First-time setup: provisioning generator infrastructure...")

    iam = boto3.client("iam", region_name=region)
    CODEBUILD_ROLE = "sap-mcp-generator-codebuild-role"

    # 1. S3 bucket
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=STAGING_BUCKET)
        else:
            s3.create_bucket(Bucket=STAGING_BUCKET,
                             CreateBucketConfiguration={"LocationConstraint": region})
        logger.info(f"Created S3 bucket: {STAGING_BUCKET}")
    except Exception:
        logger.info(f"S3 bucket already exists: {STAGING_BUCKET}")

    # 2. CodeBuild IAM role
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
        "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        role_arn = iam.create_role(RoleName=CODEBUILD_ROLE,
            AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        logger.info(f"Created CodeBuild role: {role_arn}")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=CODEBUILD_ROLE)["Role"]["Arn"]

    cb_policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account_id}:log-group:/aws/codebuild/{CODEBUILD_PROJECT}*"},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
         "Resource": [f"arn:aws:s3:::{STAGING_BUCKET}", f"arn:aws:s3:::{STAGING_BUCKET}/*"]},
        {"Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:PutParameter"],
         "Resource": f"arn:aws:ssm:{region}:{account_id}:parameter/sap_*"},
        {"Effect": "Allow", "Action": ["ecr:*"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["bedrock-agentcore:CreateAgentRuntime",
         "bedrock-agentcore:UpdateAgentRuntime", "bedrock-agentcore:GetAgentRuntime"],
         "Resource": "*"},
        {"Effect": "Allow", "Action": ["iam:CreateRole", "iam:AttachRolePolicy",
         "iam:PassRole", "iam:GetRole", "iam:PutRolePolicy"],
         "Resource": f"arn:aws:iam::{account_id}:role/sap-generated-*"},
    ]}
    iam.put_role_policy(RoleName=CODEBUILD_ROLE, PolicyName="sap-mcp-generator-policy",
                        PolicyDocument=json.dumps(cb_policy))

    # 3. CodeBuild project — buildspec inline
    import time as _time
    _time.sleep(10)  # IAM propagation

    buildspec_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "buildspec.yml")
    if os.path.exists(buildspec_path):
        with open(buildspec_path) as f:
            buildspec = f.read()
    else:
        # Inline fallback buildspec
        buildspec = _BUILDSPEC_INLINE

    try:
        cb.create_project(
            name=CODEBUILD_PROJECT,
            description="Generates and deploys SAP MCP servers to AgentCore",
            source={"type": "NO_SOURCE", "buildspec": buildspec},
            artifacts={"type": "NO_ARTIFACTS"},
            environment={"type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0",
                         "computeType": "BUILD_GENERAL1_SMALL", "privilegedMode": True,
                         "environmentVariables": [
                             {"name": "STAGING_BUCKET", "value": STAGING_BUCKET, "type": "PLAINTEXT"}]},
            serviceRole=role_arn, timeoutInMinutes=30,
        )
        logger.info(f"Created CodeBuild project: {CODEBUILD_PROJECT}")
    except cb.exceptions.ResourceAlreadyExistsException:
        logger.info(f"CodeBuild project already exists: {CODEBUILD_PROJECT}")

    # 4. Store in SSM
    okta_domain = os.environ.get("OKTA_DOMAIN", "trial-1053860.okta.com")
    okta_client_id = os.environ.get("OKTA_CLIENT_ID", "0oa10vth79kZAuXGt698")
    for key, val in {
        "/sap_smart_agent/staging_bucket": STAGING_BUCKET,
        "/sap_smart_agent/codebuild_project": CODEBUILD_PROJECT,
        "/sap_smart_agent/okta_domain": okta_domain,
        "/sap_smart_agent/okta_client_id": okta_client_id,
    }.items():
        ssm.put_parameter(Name=key, Value=val, Type="String", Overwrite=True)

    logger.info("Generator infrastructure provisioned successfully")
    return STAGING_BUCKET, CODEBUILD_PROJECT


@mcp.tool()
def generate_and_deploy_mcp_server(ctx: Context, prompt: str, agent_name: str,
                                    bearer_token: str = "") -> str:
    """Generate a new focused SAP MCP server and deploy it to AgentCore.

    The parent agent discovers relevant SAP OData services automatically based on the prompt,
    generates tool code using Claude, and deploys a new AgentCore runtime with the same Okta auth.

    Args:
        prompt: Description of what the MCP server should do (e.g. 'Plant Maintenance tools for orders and notifications')
        agent_name: Short identifier for the new server (e.g. 'sap_pm_agent'). Must be lowercase with underscores.
    """
    token = _get_token(ctx, bearer_token)
    if not token:
        return json.dumps({"error": "No bearer token"})

    try:
        boto_session = boto3.session.Session()
        region = boto_session.region_name
        ssm = boto3.client("ssm", region_name=region)
        s3 = boto3.client("s3", region_name=region)
        cb = boto3.client("codebuild", region_name=region)

        # Load config from SSM — auto-provision if first time
        staging_bucket, codebuild_project = _ensure_generator_infrastructure(region, ssm, cb, s3)

        logger.info(f"Generating MCP server '{agent_name}' for: {prompt}")

        # Step 1: Extract domain keywords from prompt and discover services
        stop_words = {"with", "that", "this", "from", "have", "will", "should", "tools", "tool",
                      "details", "including", "information", "related", "help", "about", "also",
                      "their", "them", "these", "those", "into", "like", "such", "some", "other",
                      "more", "most", "very", "just", "only", "also", "both", "each", "every"}
        keywords = [w.strip(",.;:!?") for w in prompt.lower().split()
                    if len(w.strip(",.;:!?")) > 3 and w.strip(",.;:!?") not in stop_words]
        # Deduplicate while preserving order
        seen = set()
        keywords = [k for k in keywords if not (k in seen or seen.add(k))]
        logger.info(f"Discovering services for keywords: {keywords}")
        matched_services = _discover_relevant_services(token, keywords)

        if not matched_services:
            return json.dumps({"error": f"No SAP services found matching: {prompt}. Try more specific keywords."})

        logger.info(f"Found {len(matched_services)} matching services")

        # Step 2: Get entity types for each matched service
        services_with_entities = []
        for svc in matched_services[:5]:
            svc_title = svc["Title"]
            svc_path = f"/sap/opu/odata/sap/{svc_title}"
            entities = _get_entities_for_service(svc_path, token)
            if entities:
                services_with_entities.append({
                    "service_path": svc_path,
                    "title": svc["Title"],
                    "entities": entities
                })

        if not services_with_entities:
            return json.dumps({"error": "Could not retrieve metadata for matched services."})

        # Step 3: Generate tool code with Bedrock
        logger.info("Generating tool code with Claude...")
        generated_tools = _generate_server_code_with_bedrock(prompt, services_with_entities, agent_name)

        # Step 4: Assemble full server file
        description = f"SAP MCP Server for: {prompt}"
        server_code = _SERVER_TEMPLATE.format(
            description=description,
            agent_name=agent_name,
            tools=generated_tools
        )

        requirements = "fastmcp\nhttpx\nbedrock-agentcore-starter-toolkit\nboto3\n"
        meta = {"agent_name": agent_name, "prompt": prompt, "services": services_with_entities}

        # Step 5: Upload to S3
        build_id = str(uuid.uuid4())[:8]
        prefix = f"generated/{build_id}"
        logger.info(f"Uploading to S3: s3://{staging_bucket}/{prefix}/")
        s3.put_object(Bucket=staging_bucket, Key=f"{prefix}/server.py", Body=server_code.encode())
        s3.put_object(Bucket=staging_bucket, Key=f"{prefix}/requirements.txt", Body=requirements.encode())
        s3.put_object(Bucket=staging_bucket, Key=f"{prefix}/meta.json", Body=json.dumps(meta).encode())

        # Step 6: Trigger CodeBuild
        logger.info(f"Triggering CodeBuild project: {codebuild_project}")
        build_resp = cb.start_build(
            projectName=codebuild_project,
            environmentVariablesOverride=[
                {"name": "BUILD_ID", "value": build_id, "type": "PLAINTEXT"},
                {"name": "STAGING_BUCKET", "value": staging_bucket, "type": "PLAINTEXT"},
            ]
        )
        cb_build_id = build_resp["build"]["id"]
        logger.info(f"CodeBuild started: {cb_build_id}")

        return json.dumps({
            "status": "deploying",
            "agent_name": agent_name,
            "codebuild_build_id": cb_build_id,
            "services_used": [s["service_path"] for s in services_with_entities],
            "message": (
                f"Deployment started. CodeBuild is building and deploying '{agent_name}' to AgentCore. "
                f"This takes ~10-15 minutes. Once complete, the ARN will be stored at SSM key: "
                f"/sap_generated/{agent_name}/agent_arn. "
                f"Track progress: aws codebuild batch-get-builds --ids '{cb_build_id}'"
            )
        }, indent=2)

    except Exception as e:
        logger.error(f"generate_and_deploy_mcp_server failed: {e}")
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    logger.info("=== SAP Smart MCP Server Starting ===")
    mcp.run(transport="streamable-http")
