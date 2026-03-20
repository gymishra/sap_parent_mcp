"""
Deploy SAP OData MCP Server to AgentCore Runtime with Okta OIDC inbound auth.

Before running, set these environment variables:
    OKTA_DOMAIN        - Your Okta domain (e.g. dev-12345.okta.com)
    OKTA_CLIENT_ID     - Okta application client ID (the one allowed to call this MCP server)
    AWS_DEFAULT_REGION - AWS region (e.g. us-east-1)

Usage:
    python deploy_mcp_server.py
"""
import os
import time
from boto3.session import Session
from bedrock_agentcore_starter_toolkit import Runtime

# Ensure we run from the script's own directory so the SDK finds the entrypoint files
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── Configuration ──────────────────────────────────────────────────────────────
OKTA_DOMAIN = os.environ.get("OKTA_DOMAIN", "trial-1053860.okta.com")
OKTA_CLIENT_ID = os.environ.get("OKTA_CLIENT_ID", "0oa10vth79kZAuXGt698")

# Okta OIDC discovery URL
# Issuer is https://trial-1053860.okta.com/oauth2/default
OKTA_DISCOVERY_URL = f"https://{OKTA_DOMAIN}/oauth2/default/.well-known/openid-configuration"

boto_session = Session()
region = boto_session.region_name
print(f"Region: {region}")
print(f"Okta Discovery URL: {OKTA_DISCOVERY_URL}")
print(f"Okta Client ID: {OKTA_CLIENT_ID}")

# ── Configure AgentCore Runtime ────────────────────────────────────────────────
agentcore_runtime = Runtime()

auth_config = {
    "customJWTAuthorizer": {
        "allowedClients": [OKTA_CLIENT_ID],
        "discoveryUrl": OKTA_DISCOVERY_URL,
    }
}

print("\nConfiguring AgentCore Runtime...")
response = agentcore_runtime.configure(
    entrypoint="sap_odata_mcp_server.py",
    auto_create_execution_role=True,
    auto_create_ecr=True,
    requirements_file="requirements_server.txt",
    region=region,
    authorizer_configuration=auth_config,
    protocol="MCP",
    agent_name="sap_odata_mcp_server",
)
print("Configuration completed")

# ── Launch to AgentCore Runtime ────────────────────────────────────────────────
print("\nLaunching MCP server to AgentCore Runtime (this may take several minutes)...")
launch_result = agentcore_runtime.launch(auto_update_on_conflict=True)
print(f"Agent ARN: {launch_result.agent_arn}")
print(f"Agent ID:  {launch_result.agent_id}")

# ── Wait for READY status ──────────────────────────────────────────────────────
print("\nWaiting for runtime to be READY...")
end_statuses = ["READY", "CREATE_FAILED", "DELETE_FAILED", "UPDATE_FAILED"]
while True:
    status_response = agentcore_runtime.status()
    status = status_response.endpoint["status"]
    print(f"  Status: {status}")
    if status in end_statuses:
        break
    time.sleep(10)

if status == "READY":
    print(f"\nMCP Server deployed successfully!")
    print(f"Agent ARN: {launch_result.agent_arn}")
    print(f"\nUse this ARN in your client to connect.")

    # Set request header allowlist to forward Authorization header to MCP server
    import boto3
    agentcore_client = boto3.client("bedrock-agentcore-control", region_name=region)
    try:
        agentcore_client.update_agent_runtime(
            agentRuntimeId=launch_result.agent_id,
            requestHeaderConfiguration={"headerAllowList": ["Authorization"]},
        )
        print("Authorization header allowlist configured")
    except Exception as e:
        print(f"Warning: Could not set header allowlist: {e}")
        print("You can set it manually: agentcore configure --request-header-allowlist Authorization")
else:
    print(f"\nDeployment failed with status: {status}")

# ── Store ARN for client usage ─────────────────────────────────────────────────
import boto3
import json

ssm_client = boto3.client("ssm", region_name=region)
ssm_client.put_parameter(
    Name="/sap_mcp_server/agent_arn",
    Value=launch_result.agent_arn,
    Type="String",
    Description="SAP OData MCP Server Agent ARN",
    Overwrite=True,
)
print("Agent ARN stored in SSM: /sap_mcp_server/agent_arn")
