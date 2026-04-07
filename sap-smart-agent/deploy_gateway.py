"""
Deploy AgentCore Gateway in front of the SAP Smart Agent Runtime.
No Lambda, no API Gateway — just a clean MCP URL with Okta JWT auth.

Usage:
    python deploy_gateway.py
"""
import sys, json, time, boto3
from boto3.session import Session

session = Session()
region = session.region_name
account_id = boto3.client("sts").get_caller_identity()["Account"]

gw_client  = boto3.client("bedrock-agentcore-control", region_name=region)
iam_client = boto3.client("iam", region_name=region)
ssm_client = boto3.client("ssm", region_name=region)

GATEWAY_NAME      = "ai-factory-gateway"
OKTA_DOMAIN       = "trial-1053860.okta.com"
OKTA_CLIENT_ID    = "0oa10vth79kZAuXGt698"
OKTA_DISCOVERY    = f"https://{OKTA_DOMAIN}/oauth2/default/.well-known/openid-configuration"
RUNTIME_ARN       = ssm_client.get_parameter(Name="/sap_smart_agent/agent_arn")["Parameter"]["Value"]
RUNTIME_ENDPOINT  = f"https://bedrock-agentcore.{session.region_name}.amazonaws.com/runtimes/{RUNTIME_ARN.replace(':', '%3A').replace('/', '%2F')}/invocations?qualifier=DEFAULT"
ROLE_NAME         = f"agentcore-{GATEWAY_NAME}-role"
TARGET_NAME       = "ai-factory-mcp-target"
SSM_URL_PARAM     = "/sap_smart_agent/gateway_url"
SSM_ID_PARAM      = "/sap_smart_agent/gateway_id"

print(f"Region:      {region}")
print(f"Account:     {account_id}")
print(f"Runtime ARN: {RUNTIME_ARN}")
print(f"Okta:        {OKTA_DISCOVERY}")

# ── 1. IAM Role ───────────────────────────────────────────────────────────────
print(f"\n1. IAM role: {ROLE_NAME}")
assume = {
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
policy = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Action": [
        "bedrock-agentcore:*", "bedrock:*", "iam:PassRole", "secretsmanager:GetSecretValue"
    ], "Resource": "*"}]
}
try:
    r = iam_client.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(assume))
    role_arn = r["Role"]["Arn"]
    print(f"   Created: {role_arn}")
    time.sleep(10)
except iam_client.exceptions.EntityAlreadyExistsException:
    role_arn = iam_client.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    print(f"   Exists:  {role_arn}")
iam_client.put_role_policy(RoleName=ROLE_NAME, PolicyName="GatewayPolicy",
                           PolicyDocument=json.dumps(policy))

# ── 2. Gateway ────────────────────────────────────────────────────────────────
print(f"\n2. Gateway: {GATEWAY_NAME}")
try:
    resp = gw_client.create_gateway(
        name=GATEWAY_NAME,
        roleArn=role_arn,
        protocolType="MCP",
        protocolConfiguration={"mcp": {
            "supportedVersions": ["2025-11-25", "2025-03-26", "2025-06-18"],
            "instructions": "AI Factory MCP Server — ABAP, OData, ADT, Cloud ALM, SuccessFactors tools",
        }},
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration={"customJWTAuthorizer": {
            "discoveryUrl": OKTA_DISCOVERY,
            "allowedClients": [OKTA_CLIENT_ID],
        }},
    )
    gateway_id = resp["gatewayId"]
    print(f"   Created: {gateway_id}")
except gw_client.exceptions.ConflictException:
    for gw in gw_client.list_gateways(maxResults=100)["items"]:
        if gw["name"] == GATEWAY_NAME:
            gateway_id = gw["gatewayId"]
            print(f"   Exists:  {gateway_id}")
            break

print("   Waiting for READY...")
while True:
    status = gw_client.get_gateway(gatewayIdentifier=gateway_id)["status"]
    print(f"   Status: {status}")
    if status == "READY": break
    if status in ("FAILED", "DELETE_FAILED"): sys.exit(f"Gateway failed: {status}")
    time.sleep(10)

# ── 3. Target ─────────────────────────────────────────────────────────────────
print(f"\n3. Target: {TARGET_NAME}")
try:
    t = gw_client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=TARGET_NAME,
        targetConfiguration={"mcp": {"mcpServer": {"endpoint": RUNTIME_ENDPOINT}}},
        credentialProviderConfigurations=[{"credentialProviderType": "OAUTH",
            "credentialProvider": {"oauthCredentialProvider": {
                "providerArn": f"arn:aws:bedrock-agentcore:{region}:{account_id}:token-vault/default",
                "scopes": ["openid", "email"],
            }}
        }],
    )
    target_id = t["targetId"]
    print(f"   Created: {target_id}")
except gw_client.exceptions.ConflictException:
    for t in gw_client.list_gateway_targets(gatewayIdentifier=gateway_id, maxResults=100)["items"]:
        if t["name"] == TARGET_NAME:
            target_id = t["targetId"]
            print(f"   Exists:  {target_id}")
            break

print("   Waiting for READY...")
while True:
    status = gw_client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)["status"]
    print(f"   Status: {status}")
    if status == "READY": break
    if status in ("FAILED", "DELETE_FAILED"): sys.exit(f"Target failed: {status}")
    time.sleep(10)

# ── Done ──────────────────────────────────────────────────────────────────────
gateway_url = f"https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"
print(f"\n{'='*60}")
print(f"Gateway URL: {gateway_url}")
print(f"{'='*60}")

ssm_client.put_parameter(Name=SSM_URL_PARAM, Value=gateway_url,
                         Type="String", Overwrite=True)
ssm_client.put_parameter(Name=SSM_ID_PARAM, Value=gateway_id,
                         Type="String", Overwrite=True)
print(f"Stored in SSM: {SSM_URL_PARAM}")
