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
    """Get entity types and properties for a SAP OData service.
    Also detects if service exists in catalog but is NOT activated (403)."""
    token = _get_token(ctx)
    try:
        url = f"{SAP_BASE_URL}{service_path}/$metadata"
        with httpx.Client(verify=False, timeout=30.0) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})
            if r.status_code == 403 and "/IWFND/MED/170" in r.text:
                svc_name = service_path.split("/")[-1]
                return json.dumps({
                    "error": "service_not_activated",
                    "service": svc_name,
                    "message": f"Service '{svc_name}' exists in the catalog but is NOT activated on the SAP Gateway. "
                               f"Activate it via transaction /IWFND/MAINT_SERVICE → Add Service → search for '{svc_name}'.",
                    "fallback": "Use run_sql_query to get the data directly from SAP tables instead."
                }, indent=2)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            entities = []
            for ns_uri in ["http://schemas.microsoft.com/ado/2008/09/edm",
                           "http://schemas.microsoft.com/ado/2009/11/edm"]:
                for et in root.iter(f"{{{ns_uri}}}EntityType"):
                    props = [p.get("Name") for p in et.iter(f"{{{ns_uri}}}Property")]
                    entities.append({"name": et.get("Name"), "properties": props[:15]})
            return json.dumps({"service": service_path, "entities": entities}, indent=2)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            svc_name = service_path.split("/")[-1]
            return json.dumps({
                "error": "service_not_activated",
                "service": svc_name,
                "message": f"Service '{svc_name}' is not activated. Activate via /IWFND/MAINT_SERVICE.",
                "fallback": "Use run_sql_query to get the data directly from SAP tables."
            }, indent=2)
        return json.dumps({"error": str(e)})
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def query_sap_odata(ctx: Context, service_path: str, entity_set: str,
                    top: int = 10, skip: int = 0,
                    filter_expr: str = "", select_fields: str = "") -> str:
    """Query any SAP OData entity set with optional filter and field selection.
    Detects inactive services (403) and suggests SQL fallback."""
    token = _get_token(ctx)
    params: dict = {"$format": "json", "$top": top, "$skip": skip}
    if filter_expr:   params["$filter"]  = filter_expr
    if select_fields: params["$select"]  = select_fields
    try:
        data    = _sap_get(f"{service_path}/{entity_set}", token, params)
        results = data.get("d", {}).get("results", [])
        # Track successful OData query in research history
        _add_research_step("odata", service_path, entity_set, filter_expr, select_fields, len(results))
        return json.dumps({"count": len(results), "results": results}, indent=2)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403 and "/IWFND/MED/170" in e.response.text:
            svc_name = service_path.split("/")[-1]
            _add_research_step("odata_failed", service_path, entity_set, filter_expr, select_fields, 0,
                               error=f"Service {svc_name} not activated")
            return json.dumps({
                "error": "service_not_activated",
                "service": svc_name,
                "message": f"Service '{svc_name}' exists but is NOT activated on SAP Gateway. "
                           f"Activate via /IWFND/MAINT_SERVICE.",
                "suggestion": "Use run_sql_query to get this data directly from SAP tables. "
                              f"The underlying table for {entity_set} can be queried via SQL."
            }, indent=2)
        return json.dumps({"error": str(e)})
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_sap_entity(ctx: Context, service_path: str, entity_set: str, key: str) -> str:
    """Get a single SAP OData entity by key. key e.g. \"SalesOrder='1000'\" """
    token = _get_token(ctx)
    try:
        data = _sap_get(f"{service_path}/{entity_set}({key})", token, {"$format": "json"})
        _add_research_step("odata_single", service_path, entity_set, key, "", 1)
        return json.dumps(data.get("d", data), indent=2)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return json.dumps({"error": "service_not_activated",
                               "message": f"Service not activated. Use run_sql_query instead."})
        return json.dumps({"error": str(e)})
    except Exception as e: return json.dumps({"error": str(e)})


# ── Research History ──────────────────────────────────────────────────────────
_research_history: list = []

def _add_research_step(method: str, service_or_table: str, entity_or_query: str,
                       filter_expr: str = "", select_fields: str = "",
                       result_count: int = 0, error: str = ""):
    _research_history.append({
        "method": method,
        "source": service_or_table,
        "entity": entity_or_query,
        "filter": filter_expr,
        "select": select_fields,
        "result_count": result_count,
        "error": error,
    })


# ── SQL Query via ADT (hybrid fallback) ──────────────────────────────────────

@mcp.tool()
def run_sql_query(ctx: Context, sql_query: str, max_rows: int = 100) -> str:
    """Execute a SQL SELECT on SAP database tables via ADT Data Preview.
    Use this when OData service is not available or doesn't have the fields you need.
    Examples:
      - SELECT ebeln, ebelp, matnr, menge, netpr FROM ekpo WHERE ebeln = '4500000001'
      - SELECT belnr, bukrs, lifnr, wrbtr, waers FROM bsik WHERE bukrs = '1710'
      - SELECT vbeln, posnr, matnr, kwmeng FROM vbap WHERE vbeln = '0000000001'
    """
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})
    try:
        result = _adt_sql(sql_query, token, max_rows)
        _add_research_step("sql", _extract_table_from_sql(sql_query), sql_query, "", "", len(result))
        return json.dumps({"count": len(result), "sql": sql_query, "results": result}, indent=2)
    except Exception as e:
        _add_research_step("sql_failed", _extract_table_from_sql(sql_query), sql_query, error=str(e))
        return json.dumps({"error": str(e), "sql": sql_query})


def _adt_sql(sql_query: str, token: str, max_rows: int = 100) -> list:
    """Execute SQL via ADT Data Preview — tries freestyle POST first, then sqlConsole GET."""
    with httpx.Client(verify=False, timeout=30.0) as c:
        # Method 1: freestyle POST (requires CSRF)
        try:
            csrf_resp = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                              headers={"Authorization": f"Bearer {token}", "x-csrf-token": "Fetch",
                                       "Accept": "*/*"})
            csrf = csrf_resp.headers.get("x-csrf-token", "")
            for accept_hdr in ["application/xml",
                                "application/vnd.sap.adt.datapreview.table.v1+xml",
                                "*/*"]:
                r = c.post(f"{SAP_BASE_URL}/sap/bc/adt/datapreview/freestyle",
                           content=sql_query.encode("utf-8"),
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain",
                                    "Accept": accept_hdr, "x-csrf-token": csrf},
                           params={"rowNumber": str(max_rows)})
                if r.status_code == 200:
                    return _parse_adt_data_preview(r.text)
                if r.status_code != 406:
                    break
        except Exception:
            pass
        # Method 2: sqlConsole GET (fallback)
        try:
            r = c.get(f"{SAP_BASE_URL}/sap/bc/adt/datapreview/sqlConsole",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"},
                      params={"rowNumber": str(max_rows), "sqlCommand": sql_query})
            if r.status_code == 200:
                return _parse_adt_data_preview(r.text)
        except Exception:
            pass
        # Both failed — raise with details
        raise RuntimeError(f"ADT SQL failed: freestyle={getattr(r, 'status_code', '?')}")


def _parse_adt_data_preview(xml_text: str) -> list:
    """Parse ADT data preview columnar XML into list of dicts."""
    root = ET.fromstring(xml_text)
    dp_ns = "http://www.sap.com/adt/dataPreview"
    columns = root.findall(f".//{{{dp_ns}}}columns") or root.findall(f".//{{{dp_ns}}}column")
    if not columns:
        return []
    col_names = []
    col_data = []
    for col in columns:
        meta = col.find(f"{{{dp_ns}}}metadata")
        if meta is not None:
            name = meta.get(f"{{{dp_ns}}}name", "") or meta.get("name", "")
            col_names.append(name)
        dataset = col.find(f"{{{dp_ns}}}dataSet")
        if dataset is not None:
            values = [d.text or "" for d in dataset.findall(f"{{{dp_ns}}}data")]
            if not values:
                values = [d.text or "" for d in dataset]
            col_data.append(values)
        else:
            col_data.append([])
    # Transpose columns to rows
    num_rows = max(len(cd) for cd in col_data) if col_data else 0
    rows = []
    for i in range(num_rows):
        row = {}
        for j, name in enumerate(col_names):
            row[name] = col_data[j][i] if j < len(col_data) and i < len(col_data[j]) else ""
        rows.append(row)
    return rows


def _extract_table_from_sql(sql: str) -> str:
    """Extract table name from a SQL SELECT statement."""
    parts = sql.upper().split("FROM")
    if len(parts) > 1:
        table = parts[1].strip().split()[0].strip()
        return table
    return "UNKNOWN"


# ── Research Summary (for generator agent) ────────────────────────────────────

@mcp.tool()
def get_research_summary(ctx: Context) -> str:
    """Get the research history from this session — all OData queries, SQL statements,
    services tried, tables accessed, and what worked vs failed.
    Use this to feed into the generator agent when creating a dedicated MCP server."""
    successful = [s for s in _research_history if not s.get("error")]
    failed = [s for s in _research_history if s.get("error")]
    odata_sources = list(set(s["source"] for s in successful if s["method"].startswith("odata")))
    sql_sources = list(set(s["source"] for s in successful if s["method"] == "sql"))
    sql_queries = [s["entity"] for s in successful if s["method"] == "sql"]
    inactive_services = list(set(s["source"] for s in failed if "not_activated" in s.get("error", "")))
    return json.dumps({
        "total_steps": len(_research_history),
        "successful": len(successful),
        "failed": len(failed),
        "odata_services_used": odata_sources,
        "sql_tables_used": sql_sources,
        "sql_queries_used": sql_queries,
        "inactive_services": inactive_services,
        "full_history": _research_history,
        "recommendation": "hybrid" if odata_sources and sql_sources else
                          "odata_only" if odata_sources else
                          "sql_only" if sql_sources else "no_data"
    }, indent=2)


@mcp.tool()
def clear_research_history(ctx: Context) -> str:
    """Clear the research history for a fresh session."""
    _research_history.clear()
    return json.dumps({"status": "cleared"})


@mcp.tool()
def smart_query(ctx: Context, question: str, max_rows: int = 100) -> str:
    """Answer a natural language question about SAP data using hybrid approach.
    Tries OData first, falls back to SQL if service is inactive or data is incomplete.
    Tracks all steps in research history for later agent generation.
    """
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})
    import boto3
    bedrock = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name)
    system = (
        "You are a SAP data expert. Given a natural language question, generate a data retrieval plan.\n"
        "Return a JSON array of steps. Each step is one of:\n"
        '  {"method":"odata","service_path":"...","entity_set":"...","filter":"...","select":"..."}\n'
        '  {"method":"sql","query":"SELECT ... FROM ... WHERE ..."}\n'
        "Use OData when a standard API exists. Use SQL for raw table access or when OData is limited.\n"
        "You can combine both — e.g., OData for PO headers + SQL for invoice line items.\n"
        "Return ONLY valid JSON array, no explanation."
    )
    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 2048,
                         "system": system, "messages": [{"role": "user", "content": question}]}),
        contentType="application/json", accept="application/json")
    raw = json.loads(resp["body"].read())["content"][0]["text"]
    # Parse — handle both single object and array
    try:
        plan = json.loads(raw)
        if isinstance(plan, dict): plan = [plan]
    except json.JSONDecodeError:
        return json.dumps({"error": "Could not parse plan", "raw": raw})

    all_results = []
    for step in plan:
        try:
            if step.get("method") == "sql":
                rows = _adt_sql(step["query"], token, max_rows)
                _add_research_step("sql", _extract_table_from_sql(step["query"]),
                                   step["query"], "", "", len(rows))
                all_results.append({"method": "sql", "query": step["query"],
                                    "count": len(rows), "data": rows})
            else:
                params: dict = {"$format": "json", "$top": max_rows}
                if step.get("filter"): params["$filter"] = step["filter"]
                if step.get("select"): params["$select"] = step["select"]
                try:
                    data = _sap_get(f"{step['service_path']}/{step['entity_set']}", token, params)
                    results = data.get("d", {}).get("results", [])
                    _add_research_step("odata", step["service_path"], step["entity_set"],
                                       step.get("filter", ""), step.get("select", ""), len(results))
                    all_results.append({"method": "odata", "service": step["service_path"],
                                        "entity": step["entity_set"], "count": len(results), "data": results})
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 403:
                        svc = step["service_path"].split("/")[-1]
                        _add_research_step("odata_failed", step["service_path"], step["entity_set"],
                                           error=f"Service {svc} not activated")
                        all_results.append({"method": "odata_failed", "service": svc,
                                            "message": f"Service {svc} not activated. Activate via /IWFND/MAINT_SERVICE.",
                                            "fallback": "Will try SQL next."})
                    else:
                        raise
        except Exception as e:
            all_results.append({"method": step.get("method", "unknown"), "error": str(e)})

    return json.dumps({"steps": len(all_results), "results": all_results}, indent=2)


def create_odata_strands_agent() -> Agent:
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    client = MCPClient(lambda: streamablehttp_client("http://localhost:8102/mcp"))
    with client:
        return Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=16000),
            tools=client.tools,
            system_prompt=(
                "You are the Hybrid Data Research Agent within the AI Factory MCP Server.\n\n"
                "Your role is to find SAP data using the BEST available method — OData APIs, SQL queries, or both.\n\n"
                "WORKFLOW:\n"
                "1. Try OData first: search_sap_services → get_service_metadata → query_sap_odata\n"
                "2. If OData service is NOT ACTIVATED (403 error):\n"
                "   - Tell the user: 'Service X exists but is not activated. Activate via /IWFND/MAINT_SERVICE.'\n"
                "   - Fall back to SQL: use run_sql_query with the appropriate SAP table\n"
                "3. If OData returns partial data (missing fields/details):\n"
                "   - Use run_sql_query to get the missing data from SAP tables\n"
                "   - Merge both results in your response\n"
                "4. For complex questions: use smart_query which auto-plans OData + SQL steps\n\n"
                "COMMON SAP TABLE MAPPINGS:\n"
                "- Sales orders: VBAK (header), VBAP (items)\n"
                "- Purchase orders: EKKO (header), EKPO (items)\n"
                "- Vendor invoices: RBKP (header), RSEG (items)\n"
                "- AP open items: BSIK, Cleared: BSAK\n"
                "- AR open items: BSID, Cleared: BSAD\n"
                "- Accounting docs: BKPF (header), BSEG (items)\n"
                "- Materials: MARA (general), MARC (plant), MARD (storage)\n"
                "- Customers: KNA1, Vendors: LFA1\n"
                "- Deliveries: LIKP (header), LIPS (items)\n\n"
                "RESEARCH TRACKING:\n"
                "- All your queries (OData + SQL) are tracked in research history\n"
                "- After answering, ask: 'Would you like me to create a dedicated AI agent for this?'\n"
                "- If yes, call get_research_summary to get the history, then hand off to generator_agent_tool\n\n"
                "CDS VIEW CREATION (production-safe path):\n"
                "When SQL was needed because no OData service exists:\n"
                "1. Tell user: 'This data came from SQL. For production use, I can create a CDS view with OData.'\n"
                "2. If user agrees, use the adt_agent_tool to call create_odata_service with the SQL tables/fields\n"
                "3. Tell user: 'CDS view created. Please activate service [NAME]_CDS in /IWFND/MAINT_SERVICE → Add Service → LOCAL'\n"
                "4. Once user confirms activation, test the new OData endpoint via query_sap_odata\n"
                "5. If test passes, ask: 'Service is live. Want me to create a dedicated MCP agent using this OData service?'\n"
                "6. If yes, feed research history (now with the new OData service) to generator_agent_tool\n"
                "7. The generated MCP agent uses ONLY OData — no ADT/SQL needed in production\n\n"
                "IMPORTANT: Always present data clearly. If you used both OData and SQL, explain which "
                "data came from where so the user understands the data sources."
            )
        )


if __name__ == "__main__":
    logger.info("=== SAP OData Sub-Agent starting on port 8102 ===")
    mcp.run(transport="streamable-http")
