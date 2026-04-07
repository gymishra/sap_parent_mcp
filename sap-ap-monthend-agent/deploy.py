"""
Deploy SAP AP Month-End Close MCP Server to AgentCore Runtime.
Same pattern as sap-smart-agent/deploy_smart_server.py — uses bedrock_agentcore_starter_toolkit.
Run from the sap-ap-monthend-agent directory:
    python deploy.py
"""
import os, time, boto3
from boto3.session import Session
from bedrock_agentcore_starter_toolkit import Runtime

os.chdir(os.path.dirname(os.path.abspath(__file__)))

AGENT_NAME     = "sap_ap_monthend_agent"
SSM_PARAM      = f"/sap_generated/{AGENT_NAME}/agent_arn"
ENTRYPOINT     = "sap_ap_monthend_mcp_server.py"
REQUIREMENTS   = "requirements_server.txt"
OKTA_DOMAIN    = os.environ.get("OKTA_DOMAIN",    "trial-1053860.okta.com")
OKTA_CLIENT_ID = os.environ.get("OKTA_CLIENT_ID", "0oa10vth79kZAuXGt698")
OKTA_DISCOVERY = f"https://{OKTA_DOMAIN}/oauth2/default/.well-known/openid-configuration"

boto_session = Session()
region = boto_session.region_name
print(f"Region  : {region}")
print(f"Agent   : {AGENT_NAME}")
print(f"SSM key : {SSM_PARAM}")

# Verify files exist
for f in [ENTRYPOINT, REQUIREMENTS]:
    size = os.path.getsize(f) if os.path.exists(f) else -1
    print(f"  {'OK' if size >= 0 else 'MISSING'} {f} ({size} bytes)")

runtime = Runtime()

auth_config = {
    "customJWTAuthorizer": {
        "allowedClients": [OKTA_CLIENT_ID],
        "discoveryUrl": OKTA_DISCOVERY,
    }
}

print("\nConfiguring AgentCore Runtime...")
runtime.configure(
    entrypoint=ENTRYPOINT,
    auto_create_execution_role=True,
    auto_create_ecr=True,
    requirements_file=REQUIREMENTS,
    region=region,
    authorizer_configuration=auth_config,
    protocol="MCP",
    agent_name=AGENT_NAME,
)

print("Launching via CodeBuild (~10-15 min)...")
result = runtime.launch(auto_update_on_conflict=True)
print(f"Agent ARN : {result.agent_arn}")

print("Waiting for READY...")
while True:
    status = runtime.status().endpoint["status"]
    print(f"  Status: {status}")
    if status in ["READY", "CREATE_FAILED", "UPDATE_FAILED"]:
        break
    time.sleep(10)

if status == "READY":
    ssm = boto3.client("ssm", region_name=region)
    ssm.put_parameter(Name=SSM_PARAM, Value=result.agent_arn, Type="String", Overwrite=True)
    print(f"\nDeployed successfully!")
    print(f"ARN : {result.agent_arn}")
    print(f"SSM : {SSM_PARAM}")
else:
    print(f"\nDeployment failed with status: {status}")
