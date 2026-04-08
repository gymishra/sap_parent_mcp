"""Stdio wrapper for SAP Credit Memo MCP Server — runs directly without bridge."""
import os, sys, json, asyncio

# Set SAP token from Okta cache before importing the server
def _load_token():
    import base64, time
    for path in [
        os.path.join(os.path.dirname(__file__), '..', 'sap-smart-agent', '.okta_token_cache.json'),
        os.path.join(os.path.dirname(__file__), '..', 'sap-smart-agent', 'agents', '.local_bridge_token_cache.json'),
    ]:
        try:
            with open(path) as f:
                token = json.load(f).get('access_token', '')
            if token:
                p = token.split('.')[1]; p += '=' * (4 - len(p) % 4)
                if json.loads(base64.b64decode(p)).get('exp', 0) > time.time() + 60:
                    os.environ['SAP_BEARER_TOKEN'] = token
                    return
        except Exception:
            continue

_load_token()

# Now import and run the server in stdio mode
sys.path.insert(0, os.path.dirname(__file__))
from sap_credit_memo_mcp_server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
