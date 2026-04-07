import asyncio, json, base64, time
from datetime import timedelta
from boto3.session import Session
import boto3
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

with open(".okta_token_cache.json") as f:
    token = json.load(f).get("access_token", "")
p = token.split(".")[1]
p += "=" * (4 - len(p) % 4)
exp = json.loads(base64.b64decode(p)).get("exp", 0)
print(f"Token valid for {int(exp - time.time())}s")

s = Session()
arn = boto3.client("ssm", region_name=s.region_name).get_parameter(
    Name="/sap_smart_agent/agent_arn")["Parameter"]["Value"]
enc = arn.replace(":", "%3A").replace("/", "%2F")
url = f"https://bedrock-agentcore.{s.region_name}.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT"

async def main():
    headers = {
        "authorization": f"Bearer {token}",
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken": token,
        "Content-Type": "application/json",
    }
    async with streamablehttp_client(url, headers, timeout=timedelta(seconds=300),
                                     terminate_on_close=False) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            print("Connected. Calling generate_and_deploy_mcp_server...")
            result = await session.call_tool("generate_and_deploy_mcp_server", arguments={
                "prompt": "Purchase order invoice verification 3-way matching POs goods receipts supplier invoices using API_PURCHASEORDER_PROCESS_SRV API_MATERIAL_DOCUMENT_SRV API_SUPPLIERINVOICE_PROCESS_SRV",
                "agent_name": "sap_po_invoice_verify_agent",
            })
            for c in result.content:
                if hasattr(c, "text"):
                    print(c.text)

asyncio.run(main())
