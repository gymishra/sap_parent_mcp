"""Deploy SAP Smart MCP Server to AgentCore Runtime."""
import os, time, boto3, json
from boto3.session import Session
from bedrock_agentcore_starter_toolkit import Runtime

os.chdir(os.path.dirname(os.path.abspath(__file__)))

OKTA_DOMAIN = os.environ.get("OKTA_DOMAIN", "trial-1053860.okta.com")
OKTA_CLIENT_ID = os.environ.get("OKTA_CLIENT_ID", "0oa10vth79kZAuXGt698")
OKTA_DISCOVERY_URL = f"https://{OKTA_DOMAIN}/oauth2/default/.well-known/openid-configuration"

boto_session = Session()
region = boto_session.region_name
print(f"Region: {region}")

runtime = Runtime()
auth_config = {
    "customJWTAuthorizer": {
        "allowedClients": [OKTA_CLIENT_ID],
        "discoveryUrl": OKTA_DISCOVERY_URL,
    }
}

print("Configuring AgentCore Runtime...")
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

print("Launching...")
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
    print(f"\nSAP Smart Agent deployed!")
    print(f"ARN: {result.agent_arn}")
    ssm = boto3.client("ssm", region_name=region)
    ssm.put_parameter(Name="/sap_smart_agent/agent_arn", Value=result.agent_arn,
                      Type="String", Overwrite=True)
    print("ARN stored in SSM: /sap_smart_agent/agent_arn")
