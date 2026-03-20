"""MCP server that shares knowledge about the SAP Smart MCP Server architecture."""
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

server = Server("sap-mcp-docs")

# ── Architecture knowledge base ──────────────────────────────────────────────

ARCHITECTURE = {
    "overview": {
        "title": "SAP Smart MCP Server — Overview",
        "content": """
The SAP Smart MCP Server is a Model Context Protocol (MCP) server that connects
AI assistants (like Kiro) to a live SAP S/4HANA system. It runs on Amazon Bedrock
AgentCore and exposes SAP OData and ADT APIs as MCP tools.

Key components:
- sap_smart_mcp_server.py — The main MCP server deployed to AgentCore
- kiro_bridge.py — Local stdio-to-StreamableHTTP bridge for Kiro connectivity
- Okta — OAuth2 authorization code flow for authentication
- AWS SSM — Stores the AgentCore runtime ARN
- AWS CodeBuild — Builds and deploys Docker images to ECR

The server uses the Bedrock AgentCore SDK and runs as a Docker container on
AgentCore's managed runtime with MCP protocol support.
"""
    },
    "tools": {
        "title": "Available MCP Tools",
        "content": """
The server exposes 8 tools:

1. call_adt_api — Call SAP ADT REST API for ABAP development (search objects,
   read/write source code, lock/unlock, activate, syntax check, unit tests,
   SQL queries, transports, DDIC access)

2. discover_sap_services — Discover all available OData services from the SAP
   service catalog (/sap/opu/odata/IWFND/CATALOGSERVICE)

3. get_service_metadata — Get entity types and properties ($metadata) for any
   SAP OData service

4. query_sap_odata — Query any SAP OData entity set with $top, $skip, $filter,
   $select support

5. get_sap_entity — Get a single SAP entity by its key

6. update_sap_entity — Update a SAP entity via PATCH (handles CSRF + ETag)

7. create_sap_entity — Create a new SAP entity via POST (handles CSRF)

8. generate_and_deploy_mcp_server — Generate a new focused SAP MCP server using
   Claude, then build and deploy it to AgentCore via CodeBuild
"""
    },
    "bridge": {
        "title": "Kiro Bridge Pattern",
        "content": """
The kiro_bridge.py acts as a local stdio MCP server that proxies tool calls to
the remote AgentCore-hosted MCP server via Streamable HTTP.

Flow:
1. Kiro connects to the bridge over stdio (standard MCP transport)
2. On each tool call, the bridge authenticates with Okta (token cached locally)
3. Resolves the AgentCore runtime URL from AWS SSM parameter
4. Opens a Streamable HTTP MCP session to AgentCore
5. Forwards the tool call and returns the result to Kiro

The bridge injects the Okta bearer token into every tool call because AgentCore
doesn't forward the auth header into the tool execution context.

Token caching: Tokens are stored in .okta_token_cache.json and reused until
60 seconds before expiry. The Okta flow uses authorization code grant with a
local HTTP callback server on port 8086.
"""
    },
    "deployment": {
        "title": "Deployment Architecture",
        "content": """
Runtime: Amazon Bedrock AgentCore (managed MCP runtime)
Container: Docker (python3.13-bookworm-slim via uv)
Platform: linux/arm64
Network: PUBLIC mode
Protocol: MCP (Streamable HTTP)
Observability: Enabled (OpenTelemetry instrumented)

ECR Repository: bedrock-agentcore-sap_smart_agent
CodeBuild Project: bedrock-agentcore-sap_smart_agent-builder
Region: us-east-1

Auth: Okta JWT authorizer with OpenID Connect discovery
  - Discovery URL: https://trial-1053860.okta.com/oauth2/default/.well-known/openid-configuration

Dependencies: mcp>=1.10.0, httpx, boto3, uvicorn,
  bedrock-agentcore<=0.1.5, bedrock-agentcore-starter-toolkit==0.1.14

The deploy_smart_server.py script handles packaging, ECR push, and AgentCore
runtime registration. CodeBuild can also be used for CI/CD builds.
"""
    },
    "sap_connection": {
        "title": "SAP System Connection",
        "content": """
The server connects to SAP S/4HANA via OData and ADT REST APIs.

OData base path: /sap/opu/odata/sap/<SERVICE_NAME>
ADT base path: /sap/bc/adt/...
Service catalog: /sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection

Authentication to SAP: Basic auth (credentials stored in environment variables
SAP_BASE_URL, SAP_USERNAME, SAP_PASSWORD on the AgentCore runtime).

CSRF handling: For write operations (POST/PUT/PATCH/DELETE), the server first
fetches a CSRF token via a GET request with x-csrf-token: Fetch header, then
includes it in the mutating request.

The server also caches the service catalog and metadata responses to reduce
repeated calls to SAP.
"""
    },
    "generator": {
        "title": "MCP Server Generator",
        "content": """
The generate_and_deploy_mcp_server tool creates new focused MCP servers:

1. Takes a natural language prompt describing the desired server
2. Discovers relevant SAP OData services by matching domain keywords
3. Fetches entity metadata for matched services
4. Calls Claude (via Bedrock) to generate Python MCP server code
5. Creates a CodeBuild project (if needed) with proper IAM roles
6. Packages the generated code and uploads to S3
7. Triggers CodeBuild to build a Docker image and push to ECR
8. Registers a new AgentCore runtime with the same Okta auth config

This enables creating domain-specific SAP agents (e.g., Plant Maintenance,
Sales, HR) without manual coding.
"""
    }
}

TIPS = """
Tips for efficient usage:
- Use $select to limit fields and save context window tokens
- Strip __deferred and __metadata from OData responses (they're noise for AI)
- Keep $top low — only fetch what you need
- Be specific in prompts: "net amount for SO 2" beats "show me SO 2 details"
- The bridge caches Okta tokens — no re-auth needed until expiry
- Use discover_sap_services first to find the right service path
- For ABAP work, call_adt_api covers everything: source code, transports, syntax checks
"""


@server.list_resources()
async def list_resources():
    resources = []
    for key, info in ARCHITECTURE.items():
        resources.append(types.Resource(
            uri=f"sap-mcp://{key}",
            name=info["title"],
            description=f"Documentation: {info['title']}",
            mimeType="text/plain"
        ))
    resources.append(types.Resource(
        uri="sap-mcp://tips",
        name="Usage Tips",
        description="Tips for efficient SAP MCP server usage",
        mimeType="text/plain"
    ))
    return resources


@server.read_resource()
async def read_resource(uri: str):
    key = str(uri).replace("sap-mcp://", "")
    if key == "tips":
        return TIPS
    if key in ARCHITECTURE:
        return ARCHITECTURE[key]["content"]
    return f"Unknown resource: {uri}"


@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="get_architecture_info",
            description="Get information about a specific aspect of the SAP MCP Server architecture. Topics: overview, tools, bridge, deployment, sap_connection, generator, tips",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic to query: overview, tools, bridge, deployment, sap_connection, generator, tips",
                        "enum": ["overview", "tools", "bridge", "deployment", "sap_connection", "generator", "tips"]
                    }
                },
                "required": ["topic"]
            }
        ),
        types.Tool(
            name="search_architecture",
            description="Search across all SAP MCP Server documentation for a keyword or phrase",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search term or phrase"
                    }
                },
                "required": ["query"]
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "get_architecture_info":
        topic = arguments.get("topic", "")
        if topic == "tips":
            return [types.TextContent(type="text", text=TIPS)]
        if topic in ARCHITECTURE:
            info = ARCHITECTURE[topic]
            return [types.TextContent(type="text", text=f"# {info['title']}\n{info['content']}")]
        return [types.TextContent(type="text", text=f"Unknown topic '{topic}'. Available: overview, tools, bridge, deployment, sap_connection, generator, tips")]

    if name == "search_architecture":
        query = arguments.get("query", "").lower()
        results = []
        for key, info in ARCHITECTURE.items():
            if query in info["content"].lower() or query in info["title"].lower():
                results.append(f"## {info['title']}\n{info['content'].strip()}")
        if query in TIPS.lower():
            results.append(f"## Usage Tips\n{TIPS.strip()}")
        if not results:
            return [types.TextContent(type="text", text=f"No results found for '{query}'")]
        return [types.TextContent(type="text", text="\n\n---\n\n".join(results))]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
