import asyncio, json
from datetime import timedelta
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import boto3
from boto3.session import Session

with open('sap-smart-agent/.okta_token_cache.json') as f:
    token = json.load(f).get('access_token', '')

s = Session()
ssm = boto3.client('ssm', region_name=s.region_name)
arn = ssm.get_parameter(Name='/sap_smart_agent/agent_arn')['Parameter']['Value']
enc = arn.replace(':', '%3A').replace('/', '%2F')
url = f'https://bedrock-agentcore.{s.region_name}.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT'
print('URL:', url[:80] + '...')

headers = {
    'authorization': f'Bearer {token}',
    'X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken': token,
    'Content-Type': 'application/json'
}

async def check():
    async with streamablehttp_client(url, headers, timeout=timedelta(seconds=30), terminate_on_close=False) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.list_tools()
            print(f'Tools found: {len(result.tools)}')
            for t in result.tools:
                print(f'  - {t.name}')

asyncio.run(check())
