"""Full test: init + list_tools + call_tool via MCP client."""
import asyncio, json, boto3, base64, time, os, sys, traceback
from datetime import timedelta
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from kiro_mcp_bridge import get_okta_token

print("Getting Okta token...")
token = get_okta_token()
p = token.split(".")[1]; p += "=" * (4 - len(p) % 4)
claims = json.loads(base64.b64decode(p))
print(f"Token OK, {int(claims['exp']-time.time())}s left, sub={claims.get('sub')}")

ssm = boto3.client("ssm", region_name="us-east-1")
arn = ssm.get_parameter(Name="/sap_mcp_server/agent_arn")["Parameter"]["Value"]
enc = arn.replace(":", "%3A").replace("/", "%2F")
url = f"https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT"

async def test():
    headers = {"authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print("Connecting...")
    try:
        async with streamablehttp_client(url, headers, timeout=timedelta(seconds=120), terminate_on_close=False) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                print("Initialized OK")
                tools = await session.list_tools()
                print(f"Tools: {[t.name for t in tools.tools]}")
                print("Calling get_sales_orders(top=2, bearer_token=...)...")
                result = await session.call_tool("get_sales_orders", arguments={"top": 2, "bearer_token": token})
                for c in result.content:
                    if hasattr(c, "text"):
                        print(c.text[:500])
    except Exception as e:
        print(f"ERROR: {e}")
        traceback.print_exc()

asyncio.run(test())
