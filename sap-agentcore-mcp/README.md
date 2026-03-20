# SAP OData MCP Server on AgentCore Runtime (Okta Auth)

## Architecture

```
User: "show me top 2 sales orders"
  │
  ▼
Python Client (sap_agent_client.py)
  │  1. Opens browser → Okta login (Authorization Code flow)
  │  2. User authenticates → callback with auth code
  │  3. Exchanges code for JWT access token (Basic Auth)
  │  4. Sends JWT as Bearer token to AgentCore
  ▼
AgentCore Runtime (Inbound Auth)
  │  Validates JWT against Okta OIDC discovery URL
  ▼
MCP Server (sap_odata_mcp_server.py)
  │  Receives bearer_token as tool parameter
  │  Calls SAP OData with that same Okta JWT
  ▼
SAP S/4HANA (https://vhcals4hci.awspoc.club)
  │  Accepts Okta JWT (OIDC configured)
  ▼
Response → MCP → Client → User sees sales orders
```

## Okta Details

| Setting | Value |
|---------|-------|
| Domain | trial-1053860.okta.com |
| Authorize URL | https://trial-1053860.okta.com/oauth2/v1/authorize |
| Token URL | https://trial-1053860.okta.com/oauth2/v1/token |
| Client ID | 0oa10vth79kZAuXGt698 |
| Grant Type | Authorization Code |
| Scopes | openid email |
| Callback (Postman) | https://oauth.pstmn.io/v1/callback |
| Callback (Python) | http://localhost:8085/callback |
| Client Auth | Basic Auth header |

## Okta App Setup

Add `http://localhost:8085/callback` as a redirect URI in your Okta app:
1. Go to Okta Admin → Applications → Your App
2. Under "Sign-in redirect URIs", add: `http://localhost:8085/callback`
3. Save

## Setup

### 1. Set environment variable
```bash
export OKTA_CLIENT_SECRET="your-okta-client-secret"
export AWS_DEFAULT_REGION="us-east-1"
```

### 2. Deploy MCP Server
```bash
cd sap-agentcore-mcp
pip install -r requirements_server.txt
python deploy_mcp_server.py
```

### 3. Run the Client
```bash
pip install -r requirements_client.txt
python sap_agent_client.py "show me top 2 sales orders"
```

A browser window will open for Okta login. After authenticating, the client
will automatically receive the token and query SAP.
