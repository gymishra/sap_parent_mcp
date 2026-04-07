"""
AI Factory Parent MCP Server
Exposes 5 Strands sub-agents as tools to Kiro via FastMCP on port 8100.

Architecture:
  Kiro → kiro_bridge → parent_mcp_server (port 8100)
           ├── adt_agent_tool()       → Strands Agent → adt_agent.py      :8101
           ├── odata_agent_tool()     → Strands Agent → odata_agent.py    :8102
           ├── calm_agent_tool()      → Strands Agent → calm_agent.py     :8103
           ├── sf_agent_tool()        → Strands Agent → sf_agent.py       :8104
           └── generator_agent_tool() → Strands Agent → generator_agent.py :8105

Sub-agents must be running before starting this server (use start_all.py).
Port: 8100
"""
import os, sys, json, logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP, Context
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ai_factory")

MODEL_ID      = "us.anthropic.claude-sonnet-4-6"
ADT_URL       = os.environ.get("ADT_AGENT_URL",       "http://localhost:8101/mcp")
ODATA_URL     = os.environ.get("ODATA_AGENT_URL",     "http://localhost:8102/mcp")
CALM_URL      = os.environ.get("CALM_AGENT_URL",      "http://localhost:8103/mcp")
SF_URL        = os.environ.get("SF_AGENT_URL",        "http://localhost:8104/mcp")
GENERATOR_URL = os.environ.get("GENERATOR_AGENT_URL", "http://localhost:8105/mcp")

mcp = FastMCP("AI Factory", host="0.0.0.0", port=8100, stateless_http=True)

# ── Pre-load token from Okta cache into env var so sub-agents can use it ──────
def _preload_token():
    import base64 as _b64, time as _t
    cache = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".okta_token_cache.json")
    try:
        with open(cache) as f:
            token = json.load(f).get("access_token", "")
        if token:
            p = token.split(".")[1]; p += "=" * (4 - len(p) % 4)
            if json.loads(_b64.b64decode(p)).get("exp", 0) > _t.time() + 60:
                os.environ["SAP_BEARER_TOKEN"] = token
                logger.info("SAP_BEARER_TOKEN pre-loaded from Okta cache")
                return
    except Exception: pass
    logger.warning("Could not pre-load token from Okta cache")

_preload_token()


def _run_sub_agent(url: str, system_prompt: str, question: str, token: str = "") -> str:
    """Connect to a sub-agent MCP server, build a Strands agent, run the question."""
    # Build headers to forward the SAP/Okta token to the sub-agent
    headers = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken"] = token

    client = MCPClient(lambda u=url, h=headers: streamablehttp_client(u, h))
    try:
        client.start()
        tools = client.list_tools_sync()
        agent = Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=16000, temperature=0.1),
            tools=tools,
            system_prompt=system_prompt,
        )
        result = agent(question)
        return str(result)
    except Exception as e:
        logger.error(f"Sub-agent error ({url}): {e}")
        return json.dumps({"error": str(e)})
    finally:
        try: client.stop()
        except Exception: pass


def _extract_token(ctx: Context) -> str:
    """Extract bearer token — tries request headers, then Okta cache, then env var."""
    # 1. Try request headers
    try:
        req = ctx.request_context.request
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken", "authorization"]:
            val = req.headers.get(h, "")
            if val:
                t = val.replace("Bearer ", "").replace("bearer ", "") \
                    if val.lower().startswith("bearer ") else val
                if t:
                    os.environ["SAP_BEARER_TOKEN"] = t  # keep env in sync
                    return t
    except Exception: pass

    # 2. Refresh from Okta cache (kiro_bridge writes here after each 3LO)
    import base64 as _b64, time as _t
    cache = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".okta_token_cache.json")
    try:
        with open(cache) as f:
            token = json.load(f).get("access_token", "")
        if token:
            p = token.split(".")[1]; p += "=" * (4 - len(p) % 4)
            if json.loads(_b64.b64decode(p)).get("exp", 0) > _t.time() + 60:
                os.environ["SAP_BEARER_TOKEN"] = token
                return token
    except Exception: pass

    # 3. Env var (set at startup or externally)
    return os.environ.get("SAP_BEARER_TOKEN", "")


# ── 5 Sub-Agent Tools ─────────────────────────────────────────────────────────

@mcp.tool()
def adt_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — ADT Agent for ABAP development and SAP object exploration.

    Use this tool when the task involves:
    - Reading or writing ABAP source code (programs, classes, function modules, includes, interfaces)
    - Understanding what an ABAP object does or how it is structured
    - Exploring CDS views and their data models
    - Running syntax checks or ATC code quality checks
    - Managing transport requests (create, release, list)
    - Querying DDIC table definitions and contents via SQL
    - Creating a new OData service via CDS view (always confirm with user first)
    - Activating an OData service in SAP Gateway
    - Listing backend services available for activation

    If an ADT API is not available as a named tool, this agent uses SAP knowledge
    to construct the correct ADT REST path and calls it directly.

    Examples: 'Show me the source code of Z_INVOICE_3WAY_MATCH',
    'What fields does table EKKO have?', 'Run ATC check on ZCL_MY_CLASS',
    'Create a transport for my changes', 'Create an OData service for purchase orders'
    """
    return _run_sub_agent(ADT_URL,
        "You are the ADT Agent — SAP ABAP development specialist. "
        "Use the most specific tool available. For data queries with no OData, "
        "generate SQL and run via get_table_contents. "
        "For OData creation: create_odata_service → activate_odata_service. "
        "Always confirm before creating or deploying anything.",
        question, _extract_token(ctx))


@mcp.tool()
def odata_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — OData Agent for SAP S/4HANA business data retrieval.

    This is the FIRST tool to use when looking for data from SAP S/4HANA.
    Use this tool when the task involves retrieving business data such as:
    - Sales orders, purchase orders, invoices, RFQs, delivery documents
    - Materials, vendors, customers, business partners
    - Stock levels, pricing conditions, accounting documents
    - Any structured business data from S/4HANA

    Workflow: search service → get metadata → query data.
    If OData exists but not active → tries to activate it.
    If no OData exists → informs caller to use adt_agent_tool for SQL query instead.
    Also used for research: discovering what OData services and entities exist.

    Examples: 'Show me open sales orders for customer 1000',
    'What OData services exist for plant maintenance?',
    'Get purchase order 4500001234 with all items'
    """
    return _run_sub_agent(ODATA_URL,
        "You are the OData Agent — SAP S/4HANA data specialist. "
        "Always try search_sap_services first, then get_service_metadata, then query. "
        "Use smart_query for complex natural language questions. "
        "If no OData exists, say so clearly so the caller can use the ADT agent for SQL.",
        question, _extract_token(ctx))


@mcp.tool()
def calm_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — Cloud ALM Agent for SAP Rise / Cloud ERP customers.

    Use this tool when the customer is on SAP Rise or SAP Cloud ERP offering.
    SAP Cloud ALM is the central operations and project management platform for Rise customers.

    Covers:
    - Implementation Projects: projects, tasks, workstreams, timeboxes, team members
    - Features: functional requirements and feature tracking
    - Documents: project documentation and specifications
    - Test Management: test cases, test activities, test actions
    - Process Hierarchy: SAP process hierarchy for implementation scope
    - Process Monitoring: real-time business process exception monitoring
    - Analytics: requirements, tasks, alerts, defects, quality gates, metrics, monitoring events
    - ITSM: support cases, landscape installations, contacts

    Examples: 'List all open tasks in my Cloud ALM project',
    'Show me process monitoring exceptions from today',
    'What features are in status In Progress?',
    'Get analytics data for defects this sprint'
    """
    return _run_sub_agent(CALM_URL,
        "You are the Cloud ALM Agent — SAP Rise/Cloud ERP specialist. "
        "For analytics, call calm_list_analytics_providers first to discover datasets, "
        "then calm_query_analytics. Use the most specific tool for each request.",
        question, _extract_token(ctx))


@mcp.tool()
def sf_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — SuccessFactors Agent for SAP HCM and HR use cases.

    Use this tool when the client needs HR or HCM capabilities from SAP SuccessFactors.
    SuccessFactors covers the full employee lifecycle.

    Covers:
    - Employee Central: headcount, personal info, employment records
    - Organizational structure: positions, departments, locations
    - Recruiting: job requisitions, candidate pipeline, hiring status
    - Learning: training completions, learning activities, certifications
    - Performance: review forms, ratings, goal achievement
    - Compensation: salary data, compensation records, pay grades
    - User management: SuccessFactors user accounts

    Always filter results when a specific employee, department, or time period is mentioned.

    Examples: 'How many employees are in the Finance department?',
    'Show me open job requisitions for engineers',
    'List employees who completed compliance training this year',
    'Get performance review data for Q4'
    """
    return _run_sub_agent(SF_URL,
        "You are the SuccessFactors Agent — SAP HCM specialist. "
        "Always use filter_ to narrow results. Use select to limit fields when only "
        "specific attributes are needed. Avoid pulling large unfiltered datasets.",
        question, _extract_token(ctx))


@mcp.tool()
def generator_agent_tool(ctx: Context, question: str) -> str:
    """AI Factory — Generator Agent for creating and deploying new SAP MCP agents.

    Use this tool when the user asks to create, build, or deploy a new focused SAP agent.
    The generator automatically detects the domain from the description and:
    1. Generates Python FastMCP server code using Claude (claude-sonnet-4-6)
    2. Packages and uploads to S3
    3. Deploys to AWS AgentCore via CodeBuild (~10-15 min)

    Domain detection:
    - Cloud ALM / Rise → calm domain (projects, monitoring, analytics tools)
    - SuccessFactors / HR → sf domain (employee, recruiting, learning tools)
    - S/4HANA / ERP data → s4 domain (auto-discovers OData services from catalog)

    Always confirms agent_name and domain before deploying.
    ARN stored in SSM at /sap_generated/{agent_name}/agent_arn when complete.

    Examples: 'Create a Cloud ALM monitoring agent for process exceptions',
    'Deploy a SuccessFactors agent for headcount reporting',
    'Build a plant maintenance agent for orders and notifications'
    """
    return _run_sub_agent(GENERATOR_URL,
        "You are the Generator Agent — SAP MCP agent factory. "
        "Use generate_and_deploy_mcp_server with a clear prompt and snake_case agent_name. "
        "Always confirm the agent_name and detected domain before deploying. "
        "Deployment takes ~10-15 minutes via CodeBuild.",
        question, _extract_token(ctx))


if __name__ == "__main__":
    logger.info("=== AI Factory Parent MCP Server starting on port 8100 ===")
    logger.info(f"ADT={ADT_URL} | OData={ODATA_URL} | CALM={CALM_URL} | SF={SF_URL} | Gen={GENERATOR_URL}")
    mcp.run(transport="streamable-http")
