"""
Local SAP OData MCP server for Kiro (stdio transport).
Runs locally, calls SAP directly with a pre-fetched Okta token.

First run: opens browser for Okta login, caches token.
Subsequent calls: reuses cached token until it expires.
"""
import os
import sys
import json
import base64
import time
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

import httpx
from mcp.server.fastmcp import FastMCP

OKTA_DOMAIN = "trial-1053860.okta.com"
OKTA_AUTHORIZE_URL = f"https://{OKTA_DOMAIN}/oauth2/default/v1/authorize"
OKTA_TOKEN_URL = f"https://{OKTA_DOMAIN}/oauth2/default/v1/token"
OKTA_CLIENT_ID = "0oa10vth79kZAuXGt698"
OKTA_CLIENT_SECRET = os.environ.get("OKTA_CLIENT_SECRET", "")
OKTA_REDIRECT_URI = "http://localhost:8085/callback"

SAP_BASE_URL = "https://vhcals4hci.awspoc.club"
SAP_ODATA_SALES_ORDER = "/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder"

TOKEN_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".okta_token_cache.json")

mcp = FastMCP("SAP OData")


# ── Token management ───────────────────────────────────────────────────────────

class CallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    def do_GET(self, *a, **k):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authenticated! Return to Kiro.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
    def log_message(self, *a): pass


def _load_cached_token() -> str | None:
    try:
        with open(TOKEN_CACHE_FILE) as f:
            data = json.load(f)
        token = data.get("access_token", "")
        if token:
            payload = token.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.b64decode(payload))
            if claims.get("exp", 0) > time.time() + 60:
                return token
    except:
        pass
    return None


def _save_token(token: str):
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump({"access_token": token}, f)


def get_token() -> str:
    cached = _load_cached_token()
    if cached:
        return cached

    if not OKTA_CLIENT_SECRET:
        raise RuntimeError("Set OKTA_CLIENT_SECRET env var")

    auth_params = {
        "client_id": OKTA_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email",
        "redirect_uri": OKTA_REDIRECT_URI,
        "state": "abc",
    }
    url = f"{OKTA_AUTHORIZE_URL}?{urllib.parse.urlencode(auth_params)}"

    server = HTTPServer(("localhost", 8085), CallbackHandler)
    CallbackHandler.auth_code = None
    t = Thread(target=server.handle_request, daemon=True)
    t.start()
    webbrowser.open(url)
    t.join(timeout=120)
    server.server_close()

    if not CallbackHandler.auth_code:
        raise RuntimeError("No auth code from Okta")

    creds = base64.b64encode(f"{OKTA_CLIENT_ID}:{OKTA_CLIENT_SECRET}".encode()).decode()
    with httpx.Client() as c:
        resp = c.post(
            OKTA_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": CallbackHandler.auth_code,
                "redirect_uri": OKTA_REDIRECT_URI,
            },
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        _save_token(token)
        return token


# ── SAP OData calls ────────────────────────────────────────────────────────────

def _call_sap(path: str, params: dict = None) -> dict:
    token = get_token()
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as client:
        resp = client.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params or {},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
def get_sales_orders(top: int = 10, skip: int = 0) -> str:
    """Get sales orders from SAP S/4HANA.

    Args:
        top: Number of sales orders to return (default 10)
        skip: Number of records to skip for pagination (default 0)
    """
    try:
        data = _call_sap(SAP_ODATA_SALES_ORDER, {"$top": str(top), "$skip": str(skip), "$format": "json"})
        results = data.get("d", {}).get("results", [])
        orders = [{
            "SalesOrder": r.get("SalesOrder"),
            "SalesOrderType": r.get("SalesOrderType"),
            "SoldToParty": r.get("SoldToParty"),
            "CreationDate": r.get("CreationDate"),
            "TotalNetAmount": r.get("TotalNetAmount"),
            "TransactionCurrency": r.get("TransactionCurrency"),
        } for r in results]
        return json.dumps(orders, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_sales_order_by_id(sales_order_id: str) -> str:
    """Get a specific sales order by ID from SAP S/4HANA.

    Args:
        sales_order_id: The sales order number (e.g. '0000000001')
    """
    try:
        data = _call_sap(f"{SAP_ODATA_SALES_ORDER}('{sales_order_id}')", {"$format": "json"})
        r = data.get("d", {})
        return json.dumps({
            "SalesOrder": r.get("SalesOrder"),
            "SalesOrderType": r.get("SalesOrderType"),
            "SoldToParty": r.get("SoldToParty"),
            "CreationDate": r.get("CreationDate"),
            "TotalNetAmount": r.get("TotalNetAmount"),
            "TransactionCurrency": r.get("TransactionCurrency"),
            "OverallSDProcessStatus": r.get("OverallSDProcessStatus"),
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run(transport="stdio")
