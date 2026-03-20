"""
Deploy Lambda MCP Bridge + API Gateway.
QuickSight → API Gateway → Lambda → AgentCore Runtime → SAP

Usage: python deploy_lambda_bridge.py
"""
import boto3
import json
import time
import zipfile
import io
import os

REGION = "us-east-1"
FUNCTION_NAME = "sap-mcp-bridge"
API_NAME = "sap-mcp-bridge-api"
ACCOUNT_ID = boto3.client("sts").get_caller_identity()["Account"]

# Get AgentCore ARN from SSM
ssm = boto3.client("ssm", region_name=REGION)
AGENTCORE_ARN = ssm.get_parameter(Name="/sap_mcp_server/agent_arn")["Parameter"]["Value"]
print(f"AgentCore ARN: {AGENTCORE_ARN}")

iam = boto3.client("iam")
lam = boto3.client("lambda", region_name=REGION)
apigw = boto3.client("apigatewayv2", region_name=REGION)

# ── 1. Create IAM Role ────────────────────────────────────────────────────────
ROLE_NAME = "sap-mcp-bridge-lambda-role"
print(f"\n1. Creating IAM role: {ROLE_NAME}")

trust_policy = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}

try:
    role = iam.create_role(
        RoleName=ROLE_NAME,
        AssumeRolePolicyDocument=json.dumps(trust_policy),
        Description="Lambda role for SAP MCP Bridge",
    )
    role_arn = role["Role"]["Arn"]
    print(f"   Created role: {role_arn}")
    # Attach policies
    iam.attach_role_policy(RoleName=ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    # Add bedrock-agentcore invoke permission
    inline_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:InvokeRuntime", "bedrock-agentcore:*"],
            "Resource": "*",
        }],
    }
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="agentcore-invoke",
        PolicyDocument=json.dumps(inline_policy))
    print("   Attached policies")
    print("   Waiting 10s for role propagation...")
    time.sleep(10)
except iam.exceptions.EntityAlreadyExistsException:
    role_arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{ROLE_NAME}"
    print(f"   Role exists: {role_arn}")

# ── 2. Create Lambda function ─────────────────────────────────────────────────
print(f"\n2. Creating Lambda function: {FUNCTION_NAME}")

# Package the lambda code
zip_buffer = io.BytesIO()
with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
    lambda_code_path = os.path.join(os.path.dirname(__file__), "lambda_mcp_bridge", "lambda_function.py")
    zf.write(lambda_code_path, "lambda_function.py")
zip_buffer.seek(0)

try:
    lam.create_function(
        FunctionName=FUNCTION_NAME,
        Runtime="python3.12",
        Role=role_arn,
        Handler="lambda_function.lambda_handler",
        Code={"ZipFile": zip_buffer.read()},
        Timeout=60,
        MemorySize=256,
        Environment={"Variables": {
            "AGENTCORE_RUNTIME_ARN": AGENTCORE_ARN,
        }},
    )
    print(f"   Created Lambda function")
except lam.exceptions.ResourceConflictException:
    # Update existing
    zip_buffer.seek(0)
    lam.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=zip_buffer.read())
    lam.update_function_configuration(
        FunctionName=FUNCTION_NAME,
        Environment={"Variables": {
            "AGENTCORE_RUNTIME_ARN": AGENTCORE_ARN,
        }},
        Timeout=60,
    )
    print(f"   Updated existing Lambda function")

# Wait for function to be active
print("   Waiting for function to be active...")
waiter = lam.get_waiter("function_active_v2")
waiter.wait(FunctionName=FUNCTION_NAME)
print("   Lambda function is active")

lambda_arn = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{FUNCTION_NAME}"

# ── 3. Create HTTP API Gateway ────────────────────────────────────────────────
print(f"\n3. Creating API Gateway: {API_NAME}")

# Check if API already exists
existing_apis = apigw.get_apis().get("Items", [])
api_id = None
for api in existing_apis:
    if api["Name"] == API_NAME:
        api_id = api["ApiId"]
        print(f"   API exists: {api_id}")
        break

if not api_id:
    api = apigw.create_api(
        Name=API_NAME,
        ProtocolType="HTTP",
        Description="SAP MCP Bridge - proxies QuickSight to AgentCore with JWT injection",
        CorsConfiguration={
            "AllowOrigins": ["*"],
            "AllowMethods": ["POST", "OPTIONS"],
            "AllowHeaders": ["*"],
        },
    )
    api_id = api["ApiId"]
    print(f"   Created API: {api_id}")

# Create Lambda integration
integration = apigw.create_integration(
    ApiId=api_id,
    IntegrationType="AWS_PROXY",
    IntegrationUri=lambda_arn,
    PayloadFormatVersion="2.0",
)
integration_id = integration["IntegrationId"]
print(f"   Created integration: {integration_id}")

# Create route for POST /mcp
try:
    apigw.create_route(
        ApiId=api_id,
        RouteKey="POST /mcp",
        Target=f"integrations/{integration_id}",
    )
    print("   Created route: POST /mcp")
except Exception as e:
    if "ConflictException" in str(type(e).__name__) or "Conflict" in str(e):
        # Update existing route
        routes = apigw.get_routes(ApiId=api_id).get("Items", [])
        for route in routes:
            if route.get("RouteKey") == "POST /mcp":
                apigw.update_route(ApiId=api_id, RouteId=route["RouteId"],
                    Target=f"integrations/{integration_id}")
                print("   Updated existing route: POST /mcp")
                break
    else:
        raise

# Create/update default stage with auto-deploy
try:
    apigw.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)
    print("   Created default stage")
except:
    apigw.update_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)
    print("   Updated default stage")

# Add Lambda invoke permission for API Gateway
try:
    lam.add_permission(
        FunctionName=FUNCTION_NAME,
        StatementId=f"apigateway-invoke-{api_id}",
        Action="lambda:InvokeFunction",
        Principal="apigateway.amazonaws.com",
        SourceArn=f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:{api_id}/*",
    )
    print("   Added Lambda invoke permission")
except lam.exceptions.ResourceConflictException:
    print("   Lambda invoke permission already exists")

api_url = f"https://{api_id}.execute-api.{REGION}.amazonaws.com"
print(f"\n{'='*60}")
print(f"DEPLOYMENT COMPLETE")
print(f"{'='*60}")
print(f"API Gateway URL: {api_url}")
print(f"MCP Endpoint:    {api_url}/mcp")
print(f"Lambda Function: {FUNCTION_NAME}")
print(f"AgentCore ARN:   {AGENTCORE_ARN}")
print(f"\nQuickSight Configuration:")
print(f"  Base URL: {api_url}/mcp")
print(f"  Auth: Custom user based OAuth (same Okta settings)")
print(f"\nTo test:")
print(f"  curl -X POST {api_url}/mcp \\")
print(f"    -H 'Authorization: Bearer <okta_jwt>' \\")
print(f"    -H 'Content-Type: application/json' \\")
print(f"    -d '{{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{{}},\"clientInfo\":{{\"name\":\"test\",\"version\":\"1.0\"}}}}}}'")
