# SAP S/4HANA + Amazon Bedrock AgentCore + Okta OIDC — Integration Guide

## Overview

This guide documents the end-to-end integration of SAP S/4HANA OData APIs with Amazon Bedrock AgentCore Runtime using Okta as the OIDC identity provider. An MCP (Model Context Protocol) server hosted on AgentCore Runtime exposes SAP OData services as tools that can be consumed by AI agents or MCP clients.

### Architecture

```
┌─────────────────┐     ┌──────────┐     ┌─────────────────────────┐     ┌──────────────────┐
│  Python Client   │────▶│   Okta   │────▶│  AgentCore Runtime      │────▶│  SAP S/4HANA     │
│  (MCP Client)    │     │  (OIDC)  │     │  (MCP Server)           │     │  (OData API)     │
│                  │◀────│          │     │                         │◀────│                  │
└─────────────────┘     └──────────┘     └─────────────────────────┘     └──────────────────┘
        │                     │                      │                           │
        │  1. Auth Code flow  │                      │                           │
        │  2. Get access_token│                      │                           │
        │                     │                      │                           │
        │  3. Bearer token ──────────────────────▶   │                           │
        │                     │  4. Validate JWT     │                           │
        │                     │     (discovery URL)  │                           │
        │                     │                      │  5. Bearer token ────────▶│
        │                     │                      │  6. OData response ◀──────│
        │  7. Sales orders ◀─────────────────────────│                           │
```

### Token Flow

The same Okta **access_token** is used for both:
- **Inbound auth**: AgentCore Runtime validates the JWT against Okta's OIDC discovery endpoint
- **SAP OData calls**: The MCP server passes the token as `Authorization: Bearer` to SAP

This is possible because SAP is configured to trust the same Okta authorization server.

---

## 1. Okta Configuration

### 1.1 Okta Application Setup

Create a Web Application in Okta with the following settings:

| Setting | Value |
|---------|-------|
| Application type | Web Application |
| Grant type | Authorization Code |
| Sign-in redirect URIs | `http://localhost:8085/callback` (Python client) |
|  | `https://oauth.pstmn.io/v1/callback` (Postman testing) |
| Sign-out redirect URIs | (optional) |
| Scopes | `openid email` |
| Client authentication | Client secret (Basic Auth) |

### 1.2 Okta Endpoints

All endpoints use the **default custom authorization server** (`/oauth2/default`):

| Endpoint | URL |
|----------|-----|
| Authorization | `https://trial-1053860.okta.com/oauth2/default/v1/authorize` |
| Token | `https://trial-1053860.okta.com/oauth2/default/v1/token` |
| OIDC Discovery | `https://trial-1053860.okta.com/oauth2/default/.well-known/openid-configuration` |
| JWKS | `https://trial-1053860.okta.com/oauth2/default/v1/keys` |
| Issuer | `https://trial-1053860.okta.com/oauth2/default` |

### 1.3 Token Details

The access_token issued by Okta contains:

| Claim | Value |
|-------|-------|
| `iss` | `https://trial-1053860.okta.com/oauth2/default` |
| `aud` | `https://trial-1053860.okta.com/oauth2/default` |
| `cid` | `0oa10vth79kZAuXGt698` (Client ID) |
| `scp` | `["openid", "email"]` |

### 1.4 Important Notes

- The **access_token** (not the id_token) is used for SAP authentication
- The token issuer MUST match what's configured in both SAP and AgentCore
- Using `/oauth2/v1/` (org auth server) produces tokens with issuer `https://trial-1053860.okta.com` — this does NOT match SAP's config
- Using `/oauth2/default/v1/` produces tokens with issuer `https://trial-1053860.okta.com/oauth2/default` — this MATCHES SAP's config

---

## 2. SAP S/4HANA Configuration

### 2.1 OIDC Provider Setup

SAP is configured to trust Okta as an OIDC identity provider with these settings:

| Setting | Value |
|---------|-------|
| Issuer | `https://trial-1053860.okta.com/oauth2/default` |
| Client ID | `0oa10vth79kZAuXGt698` |
| User Mapping Claim | `sub` claim (OIDC standard) |
| User Mapping Mechanism | E-Mail |
| JWKS Download URL | `https://trial-1053860.okta.com/oauth2/default/v1/keys` |

### 2.2 SAP OData Service

| Setting | Value |
|---------|-------|
| SAP Server | `https://vhcals4hci.awspoc.club` |
| OData Service | `/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder` |
| Authentication | Bearer token (Okta access_token) |

### 2.3 SAP Prerequisites

- SAP ICM must have HTTPS enabled
- Okta's SSL root CA certificate must be imported in `STRUST` (SSL Client PSE)
- SAP must be able to reach Okta endpoints outbound (for JWKS key download)
- If SAP shows RFC error `ThSAPOCMINIT, CM_PRODUCT_SPECIFIC_ERROR cmRc=20`, it means SAP cannot reach Okta — check network/proxy settings

---

## 3. Amazon Bedrock AgentCore Configuration

### 3.1 MCP Server (`sap_odata_mcp_server.py`)

The MCP server exposes two tools:

| Tool | Description | Parameters |
|------|-------------|------------|
| `get_sales_orders` | Get sales orders with pagination | `bearer_token`, `top`, `skip` |
| `get_sales_order_by_id` | Get a specific sales order | `bearer_token`, `sales_order_id` |

Key implementation details:
- Uses `FastMCP` with `stateless_http=True` (required for AgentCore Runtime)
- Listens on `0.0.0.0:8000/mcp`
- Makes OData calls using `httpx` with `verify=False` (for self-signed SAP certs)
- The `bearer_token` parameter is passed by the client — it's the Okta access_token

### 3.2 AgentCore Runtime Deployment (`deploy_mcp_server.py`)

Deployment configuration:

| Setting | Value |
|---------|-------|
| Agent Name | `sap_odata_mcp_server` |
| Protocol | MCP |
| Platform | linux/arm64 |
| Network Mode | PUBLIC |
| ECR | Auto-created |
| Execution Role | Auto-created |

### 3.3 Inbound Auth (JWT Authorizer)

AgentCore Runtime is configured with a custom JWT authorizer:

```python
authorizer_configuration = {
    "customJWTAuthorizer": {
        "allowedClients": ["0oa10vth79kZAuXGt698"],
        "discoveryUrl": "https://trial-1053860.okta.com/oauth2/default/.well-known/openid-configuration",
    }
}
```

AgentCore validates incoming requests by:
1. Extracting the Bearer token from the `Authorization` header
2. Fetching JWKS keys from the discovery URL
3. Validating the JWT signature, expiry, and `cid` claim against `allowedClients`

### 3.4 Deployment Steps

```bash
# Set environment
export OKTA_DOMAIN="trial-1053860.okta.com"
export OKTA_CLIENT_ID="0oa10vth79kZAuXGt698"
export AWS_DEFAULT_REGION="us-east-1"

# Deploy (requires Docker running)
cd sap-agentcore-mcp
pip install -r requirements_server.txt
python deploy_mcp_server.py
```

The deploy script:
1. Configures AgentCore Runtime with MCP protocol and Okta JWT authorizer
2. Builds a Docker image with the MCP server code
3. Pushes to ECR
4. Creates/updates the AgentCore Runtime
5. Waits for READY status
6. Stores the Agent ARN in SSM Parameter Store (`/sap_mcp_server/agent_arn`)

### 3.5 Dependencies (`requirements_server.txt`)

```
mcp>=1.10.0
httpx
boto3
bedrock-agentcore<=0.1.5
bedrock-agentcore-starter-toolkit==0.1.14
```

---

## 4. Python Client (`sap_agent_client.py`)

### 4.1 Authentication Flow

The client implements the OAuth2 Authorization Code flow:

1. Starts a local HTTP server on `localhost:8085` for the callback
2. Opens the browser to Okta's authorize endpoint
3. User authenticates with Okta (MFA supported)
4. Okta redirects to `localhost:8085/callback` with an authorization code
5. Client exchanges the code for tokens using Basic Auth (client_id:client_secret)
6. Uses the **access_token** for both AgentCore and SAP

### 4.2 MCP Connection

The client connects to AgentCore Runtime via Streamable HTTP:

```python
mcp_url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"

headers = {
    "authorization": f"Bearer {access_token}",
    "Content-Type": "application/json",
}
```

### 4.3 Tool Invocation

The client calls MCP tools with the access_token passed as a parameter:

```python
result = await session.call_tool(
    "get_sales_orders",
    arguments={
        "bearer_token": access_token,  # Same token used for AgentCore inbound auth
        "top": 2,
    },
)
```

### 4.4 Running the Client

```powershell
$env:OKTA_CLIENT_SECRET="<your-secret>"
cd sap-agentcore-mcp
pip install -r requirements_client.txt
python sap_agent_client.py "show me top 2 sales orders"
```

---

## 5. Local Testing (`test_sap_local.py`)

A standalone script that bypasses AgentCore to test the Okta → SAP connection directly:

```powershell
$env:OKTA_CLIENT_SECRET="<your-secret>"
python test_sap_local.py
```

This script:
1. Performs the same Okta Authorization Code flow
2. Prints full token contents (access_token and id_token) with decoded claims
3. Calls SAP OData directly with the access_token
4. Useful for debugging token/SAP issues without AgentCore in the middle

---

## 6. Troubleshooting

### 401 from AgentCore Runtime
- **Cause**: Token issuer doesn't match the discovery URL configured in AgentCore
- **Fix**: Ensure the Okta authorize/token URLs use `/oauth2/default/v1/` (not `/oauth2/v1/`)
- **Verify**: Decode the access_token and check the `iss` claim matches the discovery URL's issuer

### 401 from SAP
- **Cause 1**: Wrong token type — SAP expects the access_token, not the id_token
- **Cause 2**: Token issuer doesn't match SAP's OIDC config (must be `https://trial-1053860.okta.com/oauth2/default`)
- **Cause 3**: SAP can't reach Okta to download JWKS keys (RFC error `cmRc=20`)
- **Fix**: Use `test_sap_local.py` to test the token directly against SAP

### CodeBuild Failed during deployment
- **Cause**: Dependency version mismatch in requirements_server.txt
- **Fix**: Use pinned versions matching the AgentCore tutorials: `bedrock-agentcore<=0.1.5`, `bedrock-agentcore-starter-toolkit==0.1.14`

### Agent already exists (ConflictException)
- **Cause**: Re-deploying without updating
- **Fix**: Use `auto_update_on_conflict=True` in `agentcore_runtime.launch()`

### Environment variable not persisting
- **PowerShell**: Use `$env:OKTA_CLIENT_SECRET="value"`
- **CMD**: Use `set OKTA_CLIENT_SECRET=value`
- Must be set in the same terminal session as the python command

---

## 7. File Reference

| File | Purpose |
|------|---------|
| `sap_odata_mcp_server.py` | MCP server with SAP OData tools (deployed to AgentCore) |
| `deploy_mcp_server.py` | Deploys MCP server to AgentCore Runtime with Okta auth |
| `sap_agent_client.py` | Python client — Okta login → AgentCore → SAP |
| `test_sap_local.py` | Local test — Okta login → SAP directly (no AgentCore) |
| `check_token.py` | Debug utility — prints token claims |
| `requirements_server.txt` | Server-side dependencies |
| `requirements_client.txt` | Client-side dependencies |
