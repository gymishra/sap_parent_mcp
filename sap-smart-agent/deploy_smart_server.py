"""
Deploy AI Factory MCP Server to AgentCore Runtime.
Copies calm_tools.py and sf_tools.py next to the entrypoint so they
are included in the Docker image built by the AgentCore toolkit.
"""
import os, time, shutil, boto3
from boto3.session import Session
from bedrock_agentcore_starter_toolkit import Runtime

os.chdir(os.path.dirname(os.path.abspath(__file__)))

OKTA_DOMAIN    = os.environ.get("OKTA_DOMAIN",    "trial-1053860.okta.com")
OKTA_CLIENT_ID = os.environ.get("OKTA_CLIENT_ID", "0oa10vth79kZAuXGt698")
OKTA_DISCOVERY = f"https://{OKTA_DOMAIN}/oauth2/default/.well-known/openid-configuration"

boto_session = Session()
region = boto_session.region_name
print(f"Region: {region}")

# calm_tools.py and sf_tools.py are already in the same directory — no copy needed.
# The toolkit packages the entire directory containing the entrypoint.
print("Files to deploy:")
for f in ["sap_smart_mcp_server.py", "calm_tools.py", "sf_tools.py", "requirements_server.txt"]:
    exists = os.path.exists(f)
    size   = os.path.getsize(f) if exists else 0
    print(f"  {'OK' if exists else 'MISSING'} {f} ({size} bytes)")

runtime = Runtime()
auth_config = {
    "customJWTAuthorizer": {
        "allowedClients": [OKTA_CLIENT_ID],
        "discoveryUrl": OKTA_DISCOVERY,
    }
}

print("\nConfiguring AgentCore Runtime...")
runtime.configure(
    entrypoint="sap_smart_mcp_server.py",
    auto_create_execution_role=True,
    auto_create_ecr=True,
    requirements_file="requirements_server.txt",
    region=region,
    authorizer_configuration=auth_config,
    protocol="MCP",
    agent_name="sap_smart_agent",
)

print("Launching (this builds a Docker image and deploys — ~10-15 min)...")
result = runtime.launch(auto_update_on_conflict=True)
print(f"Agent ARN: {result.agent_arn}")

print("Waiting for READY...")
while True:
    status = runtime.status().endpoint["status"]
    print(f"  Status: {status}")
    if status in ["READY", "CREATE_FAILED", "UPDATE_FAILED"]:
        break
    time.sleep(10)

if status == "READY":
    print(f"\nAI Factory MCP Server deployed successfully!")
    print(f"ARN: {result.agent_arn}")
    ssm = boto3.client("ssm", region_name=region)
    ssm.put_parameter(Name="/sap_smart_agent/agent_arn", Value=result.agent_arn,
                      Type="String", Overwrite=True)
    print("ARN stored in SSM: /sap_smart_agent/agent_arn")
    print(f"\nUpdate mcp.json 'ai-factory' entry — remove LOCAL_MCP_URL to use AgentCore.")
else:
    print(f"\nDeployment failed: {status}")
