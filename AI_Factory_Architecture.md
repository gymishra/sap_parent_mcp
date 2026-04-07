# AI Factory MCP Server — High-Level Architecture

## Overview

The AI Factory is a multi-agent SAP AI platform built on AWS Bedrock AgentCore and the Model Context Protocol (MCP). It serves two distinct purposes:

1. **Research & Discovery (Non-Production)** — Explore SAP system capabilities, discover OData services, inspect ABAP objects, and prototype new agent behaviors against a non-production SAP environment
2. **Targeted AI Agents (Production)** — Generate, deploy, and operate focused agents that perform specific business actions against production SAP systems

Identity propagation uses Okta JWT (OIDC) end-to-end, ensuring the SAP user context is preserved from the IDE through to the SAP backend.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CLIENT LAYER                                      │
│                                                                             │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────────────┐     │
│   │  Kiro IDE │    │ QuickSight│    │  Claude  │    │ Custom MCP Client│     │
│   │  (Dev)   │    │ (BI/Exec)│    │ Desktop  │    │  (API Consumer)  │     │
│   └────┬─────┘    └────┬─────┘    └────┬─────┘    └────────┬─────────┘     │
│        │               │               │                    │               │
│        └───────────────┴───────────────┴────────────────────┘               │
│                                    │                                        │
│                          Okta 3LO / JWT (OIDC)                              │
└────────────────────────────────────┼────────────────────────────────────────┘
                                     │
┌────────────────────────────────────┼────────────────────────────────────────┐
│                        IDENTITY LAYER                                       │
│                                                                             │
│   ┌────────────────────────────────┴───────────────────────────────────┐    │
│   │                     Okta Identity Provider                         │    │
│   │                                                                    │    │
│   │  - Authorization Code Flow (3LO) for interactive clients           │    │
│   │  - Client Credentials Flow for service-to-service                  │    │
│   │  - JWT validation at AgentCore (OIDC discovery URL)                │    │
│   │  - Token carries SAP user identity for backend propagation         │    │
│   └────────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│                              JWT Bearer Token                               │
└────────────────────────────────────┼────────────────────────────────────────┘
                                     │
┌────────────────────────────────────┼────────────────────────────────────────┐
│                     AWS BEDROCK AGENTCORE                                    │
│                                                                             │
│   ┌────────────────────────────────┴───────────────────────────────────┐    │
│   │              AI Factory MCP Server (Research)                       │    │
│   │              AgentCore Runtime: sap_smart_agent-9fYEiV4cnV         │    │
│   │              Connected to: NON-PRODUCTION SAP                      │    │
│   │                                                                    │    │
│   │   ┌─────────────────────────────────────────────────────────┐     │    │
│   │   │              5 Strands Router Tools                      │     │    │
│   │   │                                                          │     │    │
│   │   │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │     │    │
│   │   │  │ odata_agent  │  │  adt_agent   │  │  calm_agent  │  │     │    │
│   │   │  │   _tool      │  │   _tool      │  │   _tool      │  │     │    │
│   │   │  │              │  │              │  │              │  │     │    │
│   │   │  │ 12 OData     │  │ 22 ADT/ABAP │  │ 46 Cloud ALM │  │     │    │
│   │   │  │ tools        │  │ tools        │  │ tools        │  │     │    │
│   │   │  └──────────────┘  └──────────────┘  └──────────────┘  │     │    │
│   │   │                                                          │     │    │
│   │   │  ┌──────────────┐  ┌──────────────────────────────────┐ │     │    │
│   │   │  │  sf_agent    │  │     generator_agent_tool         │ │     │    │
│   │   │  │   _tool      │  │                                  │ │     │    │
│   │   │  │              │  │  Generates + deploys new agents  │ │     │    │
│   │   │  │ 11 SF/HCM   │  │  to AgentCore via CodeBuild      │ │     │    │
│   │   │  │ tools        │  │  (S/4, Cloud ALM, SF domains)    │ │     │    │
│   │   │  └──────────────┘  └──────────────────────────────────┘ │     │    │
│   │   └─────────────────────────────────────────────────────────┘     │    │
│   └────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│   ┌────────────────────────────────────────────────────────────────────┐    │
│   │           Generated Production Agents (Targeted)                    │    │
│   │           Connected to: PRODUCTION SAP                              │    │
│   │                                                                     │    │
│   │   ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐    │    │
│   │   │ PM Maint     │  │ CALM Monitor │  │ SF HR Headcount      │    │    │
│   │   │ Agent        │  │ Agent        │  │ Agent                │    │    │
│   │   │              │  │              │  │                      │    │    │
│   │   │ Plant Maint  │  │ Process      │  │ Employee data,       │    │    │
│   │   │ orders,      │  │ exceptions,  │  │ recruiting,          │    │    │
│   │   │ notifications│  │ alerts,      │  │ performance          │    │    │
│   │   │ equipment    │  │ analytics    │  │ reviews              │    │    │
│   │   └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘    │    │
│   │          │                 │                      │               │    │
│   │          └─────────────────┴──────────────────────┘               │    │
│   │                            │                                      │    │
│   │                   Okta JWT (same identity)                        │    │
│   └────────────────────────────┼───────────────────────────────────────┘    │
│                                │                                            │
│   ┌────────────────────────────┼───────────────────────────────────────┐    │
│   │              Agent Generation Pipeline                              │    │
│   │                                                                     │    │
│   │   1. Claude (Bedrock) generates Python MCP server code              │    │
│   │   2. Code + requirements uploaded to S3 staging bucket              │    │
│   │   3. CodeBuild builds ARM64 Docker image                            │    │
│   │   4. Image pushed to ECR                                            │    │
│   │   5. AgentCore Runtime created with Okta JWT auth                   │    │
│   │   6. ARN stored in SSM: /sap_generated/{agent_name}/agent_arn       │    │
│   │                                                                     │    │
│   │   ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌────────┐ │    │
│   │   │ Bedrock │→ │   S3    │→ │CodeBuild│→ │   ECR   │→ │AgentCor│ │    │
│   │   │ Claude  │  │ Staging │  │ ARM64   │  │  Image  │  │Runtime │ │    │
│   │   └─────────┘  └─────────┘  └─────────┘  └─────────┘  └────────┘ │    │
│   └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │              Observability (OpenTelemetry)                            │   │
│   │                                                                      │   │
│   │   CloudWatch Logs → Transaction Search → GenAI Dashboard             │   │
│   │   X-Ray Traces → Agent invocation tracing                            │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                              Okta JWT Bearer
                                     │
┌────────────────────────────────────┼────────────────────────────────────────┐
│                        SAP BACKEND LAYER                                    │
│                                                                             │
│   ┌────────────────────────────────┴───────────────────────────────────┐    │
│   │                                                                    │    │
│   │   ┌─────────────────────┐         ┌─────────────────────┐         │    │
│   │   │  NON-PRODUCTION     │         │    PRODUCTION        │         │    │
│   │   │  SAP S/4HANA        │         │    SAP S/4HANA       │         │    │
│   │   │                     │         │                      │         │    │
│   │   │  Used by:           │         │  Used by:            │         │    │
│   │   │  - AI Factory       │         │  - Generated Agents  │         │    │
│   │   │    (research)       │         │    (targeted actions)│         │    │
│   │   │  - OData discovery  │         │  - PM Agent          │         │    │
│   │   │  - ABAP inspection  │         │  - Finance Agent     │         │    │
│   │   │  - CDS prototyping  │         │  - Procurement Agent │         │    │
│   │   │  - SQL exploration  │         │                      │         │    │
│   │   │                     │         │  Auth: Same Okta JWT │         │    │
│   │   │  Auth: Okta JWT     │         │  propagated via      │         │    │
│   │   │  via AgentCore      │         │  AgentCore header    │         │    │
│   │   └─────────────────────┘         └──────────────────────┘         │    │
│   │                                                                    │    │
│   │   ┌─────────────────────┐         ┌─────────────────────┐         │    │
│   │   │  SAP Cloud ALM      │         │  SAP SuccessFactors  │         │    │
│   │   │  (Rise/Cloud ERP)   │         │  (HCM Cloud)         │         │    │
│   │   │                     │         │                      │         │    │
│   │   │  OAuth2 Client      │         │  OAuth2 Client       │         │    │
│   │   │  Credentials        │         │  Credentials         │         │    │
│   │   │  (separate from     │         │  (separate from      │         │    │
│   │   │   SAP S/4 auth)     │         │   SAP S/4 auth)      │         │    │
│   │   └─────────────────────┘         └──────────────────────┘         │    │
│   │                                                                    │    │
│   └────────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Identity Propagation Flow

```
User (Kiro/QuickSight/Claude)
    │
    ▼
Okta Authorization Code Flow (3LO)
    │  - Browser redirect to Okta login
    │  - User authenticates with corporate credentials
    │  - Okta issues JWT with SAP user claims
    │
    ▼
kiro_bridge.py (stdio MCP bridge)
    │  - Caches JWT locally (.okta_token_cache.json)
    │  - Sends JWT in Authorization header
    │  - Also sends via X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken
    │
    ▼
AWS Bedrock AgentCore Runtime
    │  - Validates JWT via Okta OIDC discovery URL
    │  - Propagates token to MCP server via custom header
    │  - No token transformation — original JWT preserved
    │
    ▼
AI Factory MCP Server (sap_smart_mcp_server.py)
    │  - Extracts token from AgentCore custom header
    │  - Passes as Bearer token to SAP backend
    │
    ▼
SAP S/4HANA (Non-Prod or Prod)
    │  - Validates JWT via SAP OAuth2 / SAML trust
    │  - Maps to SAP user (SU01 user master)
    │  - Applies SAP authorization objects (S_TCODE, M_RECH_WRK, etc.)
    │  - Returns data scoped to user's authorizations
```

---

## Research vs. Production Flow

```
                    ┌─────────────────────────────────┐
                    │         AI FACTORY               │
                    │     (Research Platform)           │
                    │                                   │
                    │  Connected to: NON-PROD SAP       │
                    │                                   │
                    │  Purpose:                         │
                    │  - Discover OData services         │
                    │  - Inspect ABAP source code        │
                    │  - Prototype CDS views             │
                    │  - Test SQL queries                │
                    │  - Validate agent behavior          │
                    │  - Generate new agents              │
                    └──────────────┬────────────────────┘
                                   │
                    generator_agent_tool
                                   │
                    ┌──────────────▼────────────────────┐
                    │      AGENT GENERATION PIPELINE     │
                    │                                    │
                    │  1. User: "Create a PM agent"       │
                    │  2. AI Factory discovers services    │
                    │     on NON-PROD SAP                 │
                    │  3. Claude generates agent code      │
                    │  4. CodeBuild deploys to AgentCore   │
                    │  5. New agent configured with        │
                    │     PRODUCTION SAP endpoint          │
                    │     + same Okta JWT auth             │
                    └──────────────┬────────────────────┘
                                   │
                    ┌──────────────▼────────────────────┐
                    │     PRODUCTION AGENTS              │
                    │                                    │
                    │  Connected to: PROD SAP            │
                    │                                    │
                    │  - Focused tool set (3-7 tools)     │
                    │  - Read-only or controlled writes    │
                    │  - Same Okta JWT identity            │
                    │  - SAP authorization enforced        │
                    │  - Observability enabled              │
                    └──────────────────────────────────────┘
```

---

## Component Inventory

| Component | Purpose | Runtime | Auth |
|-----------|---------|---------|------|
| `sap_smart_mcp_server.py` | AI Factory parent — 5 router tools + 92 internal tools | AgentCore | Okta JWT |
| `calm_tools.py` | Cloud ALM API tools (42 tools) | Bundled with parent | OAuth2 CC |
| `sf_tools.py` | SuccessFactors API tools (11 tools) | Bundled with parent | OAuth2 CC |
| `kiro_bridge.py` | Stdio bridge — Okta 3LO + dynamic tool discovery | Local (Kiro) | Okta 3LO |
| `deploy_smart_server.py` | Deploys AI Factory to AgentCore | Local script | AWS IAM |
| `generator_agent.py` | Generates + deploys new focused agents | Inside parent | Bedrock + CodeBuild |
| `mcp-lambda-basic-auth.js` | Lambda proxy for API Gateway access | Lambda | Okta CC |
| Generated agents | Focused domain agents (PM, Finance, etc.) | AgentCore (each) | Okta JWT |

---

## Security Model

| Layer | Mechanism | Detail |
|-------|-----------|--------|
| Client → AgentCore | Okta JWT (OIDC) | 3LO for interactive, CC for service |
| AgentCore → MCP Server | Custom header propagation | `X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken` |
| MCP Server → SAP S/4 | Bearer token | Original Okta JWT forwarded as-is |
| MCP Server → Cloud ALM | OAuth2 Client Credentials | Separate service binding per tenant |
| MCP Server → SuccessFactors | OAuth2 Client Credentials | Company ID + client credentials |
| SAP Backend | Authorization objects | User-level access control (SU01 + roles) |
| Agent Generation | AWS IAM + Okta | CodeBuild role + Okta JWT on new runtime |

---

## Key Design Decisions

1. **Non-Prod for Research, Prod for Action** — The AI Factory explores and prototypes on non-prod. Generated agents are deployed with production SAP endpoints. This prevents accidental writes to production during discovery.

2. **Same Identity, Different Endpoints** — Both research and production use the same Okta JWT. The SAP user's authorizations control what data is accessible. The only difference is the SAP base URL.

3. **Strands Agents for Routing** — Each domain tool (`odata_agent_tool`, `adt_agent_tool`, etc.) is a Strands agent that reasons about which internal tools to call. This keeps the tool count visible to Kiro at 5 while having 92 tools available internally.

4. **Agent Generation as a Tool** — The `generator_agent_tool` is itself an MCP tool, meaning users can create new agents conversationally: "Create a plant maintenance agent" → generates code → deploys to AgentCore → ready in 10-15 minutes.

5. **No Token Transformation** — The Okta JWT flows unchanged from client to SAP. No intermediate token exchange, no service accounts. The SAP backend sees the actual user identity.
