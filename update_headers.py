import boto3
client = boto3.client('bedrock-agentcore-control', region_name='us-east-1')

# Update both agents with header allowlist
for agent_id, container in [
    ('sap_credit_memo_agent-eYb9d43l2E', '953841955037.dkr.ecr.us-east-1.amazonaws.com/bedrock-agentcore-sap_credit_memo_agent:latest'),
    ('sap_smart_agent-lHra6E8O2A', '953841955037.dkr.ecr.us-east-1.amazonaws.com/bedrock-agentcore-sap_smart_agent:latest'),
]:
    try:
        r = client.get_agent_runtime(agentRuntimeId=agent_id)
        role = r.get('roleArn', '')
        headers = r.get('requestHeaderConfiguration')
        if headers and 'requestHeaderAllowlist' in headers:
            print(f'{agent_id}: already has header allowlist')
            continue
        r2 = client.update_agent_runtime(
            agentRuntimeId=agent_id,
            agentRuntimeArtifact={'containerConfiguration': {'containerUri': container}},
            roleArn=role,
            networkConfiguration={'networkMode': 'PUBLIC'},
            requestHeaderConfiguration={
                'requestHeaderAllowlist': ['X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken']
            }
        )
        print(f'{agent_id}: updated, status={r2.get("status")}')
    except Exception as e:
        print(f'{agent_id}: {e}')
