"""
SAP OData Sub-Agent — S/4HANA OData queries, smart NL→SQL, entity discovery.
Port: 8102
"""
import os, json, logging, httpx, xml.etree.ElementTree as ET
import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP, Context
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("odata_agent")

MODEL_ID     = "us.anthropic.claude-sonnet-4-6"
SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
CATALOG_URL  = "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"

mcp = FastMCP("AI Factory — OData Agent", host="0.0.0.0", port=8102, stateless_http=True)


def _get_token(ctx: Context) -> str:
    try:
        req = ctx.request_context.request
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken", "authorization"]:
            val = req.headers.get(h, "")
            if val:
                return val.replace("Bearer ", "").replace("bearer ", "") if val.lower().startswith("bearer ") else val
    except Exception: pass
    return os.environ.get("SAP_BEARER_TOKEN", "")


def _sap_get(path: str, token: str, params: dict = None) -> dict:
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                  params=params or {})
        r.raise_for_status()
        return r.json()


@mcp.tool()
def search_sap_services(ctx: Context, keyword: str, limit: int = 20) -> str:
    """Search SAP OData services by keyword. Faster than discover_sap_services."""
    token = _get_token(ctx)
    try:
        data = _sap_get(CATALOG_URL, token,
                        {"$format": "json", "$top": 200,
                         "$filter": f"substringof('{keyword}',Title) or substringof('{keyword}',TechnicalServiceName)"})
        results = data.get("d", {}).get("results", [])[:limit]
        return json.dumps([{"Title": r["Title"], "TechnicalServiceName": r.get("TechnicalServiceName", "")}
                           for r in results], indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_service_metadata(ctx: Context, service_path: str) -> str:
    """Get entity types and properties for a SAP OData service."""
    token = _get_token(ctx)
    try:
        xml_text = _sap_get.__wrapped__ if hasattr(_sap_get, "__wrapped__") else None
        url = f"{SAP_BASE_URL}{service_path}/$metadata"
        with httpx.Client(verify=False, timeout=30.0) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})
            r.raise_for_status()
            root = ET.fromstring(r.text)
            ns = {"edm": "http://schemas.microsoft.com/ado/2008/09/edm"}
            entities = []
            for et in root.iter("{http://schemas.microsoft.com/ado/2008/09/edm}EntityType"):
                props = [p.get("Name") for p in et.iter("{http://schemas.microsoft.com/ado/2008/09/edm}Property")]
                entities.append({"name": et.get("Name"), "properties": props[:10]})
            return json.dumps({"service": service_path, "entities": entities}, indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def query_sap_odata(ctx: Context, service_path: str, entity_set: str,
                    top: int = 10, skip: int = 0,
                    filter_expr: str = "", select_fields: str = "") -> str:
    """Query any SAP OData entity set with optional filter and field selection."""
    token = _get_token(ctx)
    params: dict = {"$format": "json", "$top": top, "$skip": skip}
    if filter_expr:   params["$filter"]  = filter_expr
    if select_fields: params["$select"]  = select_fields
    try:
        data    = _sap_get(f"{service_path}/{entity_set}", token, params)
        results = data.get("d", {}).get("results", [])
        return json.dumps({"count": len(results), "results": results}, indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_sap_entity(ctx: Context, service_path: str, entity_set: str, key: str) -> str:
    """Get a single SAP OData entity by key. key e.g. \"SalesOrder='1000'\" """
    token = _get_token(ctx)
    try:
        data = _sap_get(f"{service_path}/{entity_set}({key})", token, {"$format": "json"})
        return json.dumps(data.get("d", data), indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def smart_query(ctx: Context, question: str, max_rows: int = 100, prefer: str = "auto") -> str:
    """Answer a natural language question about SAP data.
    Auto-decides between OData and SQL. prefer: auto | odata | sql
    """
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})
    import boto3
    bedrock = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name)
    system = (
        "You are a SAP data expert. Given a natural language question, generate either:\n"
        "1. An OData query: {\"method\":\"odata\",\"service_path\":\"...\",\"entity_set\":\"...\","
        "\"filter\":\"...\",\"select\":\"...\"}\n"
        "2. A SQL query: {\"method\":\"sql\",\"query\":\"SELECT ... FROM ... WHERE ...\"}\n"
        "Return ONLY valid JSON, no explanation."
    )
    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 1024,
                         "system": system, "messages": [{"role": "user", "content": question}]}),
        contentType="application/json", accept="application/json")
    plan = json.loads(json.loads(resp["body"].read())["content"][0]["text"])
    try:
        if plan.get("method") == "sql":
            import urllib.parse
            encoded = urllib.parse.quote(plan["query"])
            url = f"{SAP_BASE_URL}/sap/bc/adt/datapreview/sqlConsole?rowNumber={max_rows}&sqlCommand={encoded}"
            with httpx.Client(verify=False, timeout=30.0) as c:
                r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})
                return r.text
        else:
            params: dict = {"$format": "json", "$top": max_rows}
            if plan.get("filter"): params["$filter"] = plan["filter"]
            if plan.get("select"): params["$select"] = plan["select"]
            data = _sap_get(f"{plan['service_path']}/{plan['entity_set']}", token, params)
            return json.dumps(data.get("d", {}).get("results", []), indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "plan": plan})


def create_odata_strands_agent() -> Agent:
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    client = MCPClient(lambda: streamablehttp_client("http://localhost:8102/mcp"))
    with client:
        return Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=16000),
            tools=client.tools,
            system_prompt=(
                "You are the OData Agent within the AI Factory MCP Server. "
                "You are the FIRST tool to use whenever a user is looking for data from an SAP S/4HANA system.\n\n"
                "Use this agent when the task involves retrieving business data such as:\n"
                "- Sales orders, purchase orders, invoices, RFQs, delivery documents\n"
                "- Materials, vendors, customers, business partners\n"
                "- Stock levels, pricing conditions, accounting documents\n"
                "- Any structured business data from S/4HANA\n\n"
                "Workflow:\n"
                "1. Use search_sap_services to find the relevant OData service\n"
                "2. Use get_service_metadata to understand the entity structure\n"
                "3. Use query_sap_odata or get_sap_entity to retrieve the data\n"
                "4. For complex natural language questions, use smart_query\n\n"
                "If OData service exists but is NOT active:\n"
                "- Try to activate it automatically, or inform the user and ask if they want it activated\n\n"
                "If NO OData service exists for the requested data:\n"
                "- Do NOT try to create one — hand off to the ADT Agent to generate and run a SQL query instead\n\n"
                "This agent is also used for research: discovering what OData services exist in the system, "
                "what entities they expose, and what data is available."
            )
        )


if __name__ == "__main__":
    logger.info("=== SAP OData Sub-Agent starting on port 8102 ===")
    mcp.run(transport="streamable-http")
