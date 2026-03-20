"""
Deploy AgentCore Gateway in front of the SAP MCP Server Runtime.
Gateway handles:
  - Inbound auth (Okta JWT validation)
  - Outbound auth (bearer token injection to SAP via MCP server)

The Gateway exposes the MCP server as a target and forwards the
inbound JWT to the Runtime, which passes it to SAP.

Usage:
    python deploy_gateway.py
"""
import os
import sys
import json
import time
import boto3
from boto3.session import Session

boto_session = Session()
region = boto_session.region_name
account_id = boto3.client("sts").get_caller_identity()["Account"]

gateway_client = boto3.client("bedrock-agentcore-control", region_name=region)
iam_client = boto3.client("iam", region_name=region)

# ── Config ─────────────────────────────────────────────────────────────────────
GATEWAY_NAME = "sap-odata-gateway"
OKTA_DOMAIN = os.environ.get("OKTA_DOMAIN", "trial-1053860.okta.com")
OKTA_CLIENT_ID = os.environ.get("OKTA_CLIENT_ID", "0oa10vth79kZAuXGt698")
OKTA_DISCOVERY_URL = f"https://{OKTA_DOMAIN}/oauth2/default/.well-known/openid-configuration"

# MCP Server Runtime ARN (already deployed)
ssm_client = boto3.client("ssm", region_name=region)
RUNTIME_ARN = ssm_client.get_parameter(Name="/sap_mcp_server/agent_arn")["Parameter"]["Value"]
print(f"Region: {region}")
print(f"Account: {account_id}")
print(f"Runtime ARN: {RUNTIME_ARN}")
print(f"Okta Discovery: {OKTA_DISCOVERY_URL}")

# ── Step 1: Create Gateway IAM Role ───────────────────────────────────────────
GATEWAY_ROLE_NAME = f"agentcore-{GATEWAY_NAME}-role"
print(f"\n1. Creating Gateway IAM role: {GATEWAY_ROLE_NAME}")

assume_role_policy = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {
            "StringEquals": {"aws:SourceAccount": account_id},
            "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"}
        }
    }]
}

role_policy = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": [
            "bedrock-agentcore:*",
            "bedrock:*",
            "iam:PassRole",
            "secretsmanager:GetSecretValue",
        ],
        "Resource": "*"
    }]
}

try:
    role_resp = iam_client.create_role(
        RoleName=GATEWAY_ROLE_NAME,
        AssumeRolePolicyDocument=json.dumps(assume_role_policy),
    )
    print(f"   Role created: {role_resp['Role']['Arn']}")
    time.sleep(10)
except iam_client.exceptions.EntityAlreadyExistsException:
    role_resp = iam_client.get_role(RoleName=GATEWAY_ROLE_NAME)
    print(f"   Role exists: {role_resp['Role']['Arn']}")

iam_client.put_role_policy(
    RoleName=GATEWAY_ROLE_NAME,
    PolicyName="GatewayPolicy",
    PolicyDocument=json.dumps(role_policy),
)
gateway_role_arn = role_resp["Role"]["Arn"]

# ── Step 2: Create Gateway ────────────────────────────────────────────────────
print(f"\n2. Creating Gateway: {GATEWAY_NAME}")

try:
    gw_resp = gateway_client.create_gateway(
        name=GATEWAY_NAME,
        roleArn=gateway_role_arn,
        protocolType="MCP",
        protocolConfiguration={"mcp": {}},
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration={
            "customJWTAuthorizer": {
                "discoveryUrl": OKTA_DISCOVERY_URL,
                "allowedClients": [OKTA_CLIENT_ID],
            }
        },
    )
    gateway_id = gw_resp["gatewayId"]
    print(f"   Gateway created: {gateway_id}")
except gateway_client.exceptions.ConflictException:
    # Gateway exists, find it
    gateways = gateway_client.list_gateways(maxResults=100)
    for gw in gateways["items"]:
        if gw["name"] == GATEWAY_NAME:
            gateway_id = gw["gatewayId"]
            print(f"   Gateway exists: {gateway_id}")
            break

# Wait for READY
print("   Waiting for Gateway to be READY...")
while True:
    gw_status = gateway_client.get_gateway(gatewayIdentifier=gateway_id)
    status = gw_status["status"]
    print(f"   Status: {status}")
    if status == "READY":
        break
    elif status in ["FAILED", "DELETE_FAILED"]:
        print(f"   Gateway failed: {status}")
        sys.exit(1)
    time.sleep(10)

# ── Step 3: Add MCP Server Runtime as Gateway Target ──────────────────────────
print(f"\n3. Adding MCP Server Runtime as Gateway target")

# The target points to the MCP server on AgentCore Runtime
# with bearer token injection — the inbound JWT is forwarded as Authorization header
try:
    target_resp = gateway_client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name="sap-odata-mcp-target",
        mcpGatewayTarget={
            "mcpServerTarget": {
                "agentRuntimeArn": RUNTIME_ARN,
            }
        },
        credentialProviderConfigurations=[{
            "credentialProviderType": "GATEWAY_IAM_ROLE",
        }],
    )
    target_id = target_resp["targetId"]
    print(f"   Target created: {target_id}")
except gateway_client.exceptions.ConflictException:
    targets = gateway_client.list_gateway_targets(gatewayIdentifier=gateway_id, maxResults=100)
    for t in targets["items"]:
        if t["name"] == "sap-odata-mcp-target":
            target_id = t["targetId"]
            print(f"   Target exists: {target_id}")
            break

# Wait for target to be ready
print("   Waiting for target to be READY...")
while True:
    t_status = gateway_client.get_gateway_target(
        gatewayIdentifier=gateway_id, targetId=target_id
    )
    status = t_status["status"]
    print(f"   Status: {status}")
    if status == "READY":
        break
    elif status in ["FAILED", "DELETE_FAILED"]:
        print(f"   Target failed: {status}")
        sys.exit(1)
    time.sleep(10)

# ── Done ──────────────────────────────────────────────────────────────────────
gateway_url = f"https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"
print(f"\n{'='*60}")
print(f"Gateway deployed successfully!")
print(f"Gateway ID: {gateway_id}")
print(f"Gateway MCP URL: {gateway_url}")
print(f"{'='*60}")
print(f"\nUse this URL in QuickSight as the MCP endpoint.")
print(f"QuickSight will pass the Okta JWT → Gateway validates it →")
print(f"Gateway forwards to MCP Server Runtime → MCP calls SAP with the token.")

# Store gateway URL in SSM
ssm_client.put_parameter(
    Name="/sap_mcp_server/gateway_url",
    Value=gateway_url,
    Type="String",
    Description="SAP OData MCP Gateway URL",
    Overwrite=True,
)
ssm_client.put_parameter(
    Name="/sap_mcp_server/gateway_id",
    Value=gateway_id,
    Type="String",
    Description="SAP OData MCP Gateway ID",
    Overwrite=True,
)
print(f"Gateway URL stored in SSM: /sap_mcp_server/gateway_url")
