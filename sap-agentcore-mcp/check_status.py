import boto3
c = boto3.client('bedrock-agentcore-control', region_name='us-east-1')

# Check Runtime
r = c.get_agent_runtime(agentRuntimeId='sap_odata_mcp_server-5YgVMZ7INB')
print(f"Runtime status: {r['status']}")

# Check Gateway target
gw_id = 'gateway-quick-start-3feaa5-1anyuhb64c'
targets = c.list_gateway_targets(gatewayIdentifier=gw_id, maxResults=100)
for t in targets.get('items', []):
    print(f"Target: {t['name']}, ID: {t['targetId']}, Status: {t['status']}")
