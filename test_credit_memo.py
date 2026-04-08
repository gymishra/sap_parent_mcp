import json, httpx
import boto3
from boto3.session import Session

with open('sap-smart-agent/.okta_token_cache.json') as f:
    token = json.load(f).get('access_token', '')

s = Session()
ssm = boto3.client('ssm', region_name=s.region_name)
arn = ssm.get_parameter(Name='/sap_generated/sap_credit_memo_agent/agent_arn')['Parameter']['Value']
enc = arn.replace(':', '%3A').replace('/', '%2F')
url = f'https://bedrock-agentcore.{s.region_name}.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT'

headers = {
    'Authorization': f'Bearer {token}',
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream',
    'X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken': token,
}

# MCP initialize
init_payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "test", "version": "1.0"}}}

with httpx.Client(verify=False, timeout=30) as c:
    r = c.post(url, json=init_payload, headers=headers)
    print('Init status:', r.status_code)
    print('Init response:', r.text[:500])
