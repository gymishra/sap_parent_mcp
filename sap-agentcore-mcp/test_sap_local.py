"""
Quick local test: Get Okta token and call SAP OData directly.
This bypasses AgentCore to verify the token + SAP connectivity works from your machine.

Usage:
    set OKTA_CLIENT_SECRET=your-secret
    python test_sap_local.py
"""
import os
import json
import base64
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

import httpx

OKTA_DOMAIN = "trial-1053860.okta.com"
OKTA_AUTHORIZE_URL = f"https://{OKTA_DOMAIN}/oauth2/default/v1/authorize"
OKTA_TOKEN_URL = f"https://{OKTA_DOMAIN}/oauth2/default/v1/token"
OKTA_CLIENT_ID = "0oa10vth79kZAuXGt698"
OKTA_CLIENT_SECRET = os.environ.get("OKTA_CLIENT_SECRET", "")
OKTA_REDIRECT_URI = "http://localhost:8085/callback"

SAP_BASE_URL = "https://vhcals4hci.awspoc.club"
SAP_ODATA_PATH = "/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder"


class CallbackHandler(BaseHTTPRequestHandler):
    auth_code = None

    def do_GET(self, *a, **kw):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Got it! Return to terminal.</h2>")
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, *a):
        pass


def get_token():
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
    t = Thread(target=server.handle_request, daemon=True)
    t.start()

    print("Opening browser for Okta login...")
    webbrowser.open(url)
    t.join(timeout=120)
    server.server_close()

    if not CallbackHandler.auth_code:
        raise RuntimeError("No auth code received")

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
        token_data = resp.json()
        # SAP expects id_token, not access_token
        return token_data["access_token"], token_data["id_token"]


def main():
    print("=" * 50)
    print("Local SAP OData Test (no AgentCore)")
    print("=" * 50)

    token = get_token()
    print(f"\nTokens obtained")

    access_token, id_token = token
    print(f"Access token length: {len(access_token)}")
    print(f"ID token length: {len(id_token)}")

    print(f"\n--- FULL ACCESS TOKEN ---")
    print(access_token)
    print(f"\n--- FULL ID TOKEN ---")
    print(id_token)

    # Decode and show both token claims
    for label, t in [("ACCESS TOKEN", access_token), ("ID TOKEN", id_token)]:
        try:
            payload = t.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.b64decode(payload))
            print(f"\n--- {label} ---")
            for k in ["iss", "aud", "cid", "sub", "scp", "exp", "amr"]:
                if k in claims:
                    print(f"  {k}: {claims[k]}")
        except Exception as e:
            print(f"  Could not decode {label}: {e}")

    # Call SAP with access_token (SAP validates access_token, not id_token)
    print(f"\nCalling SAP with ACCESS token...")
    print(f"URL: {SAP_BASE_URL}{SAP_ODATA_PATH}?$top=2")
    with httpx.Client(verify=False, timeout=30.0) as client:
        resp = client.get(
            f"{SAP_BASE_URL}{SAP_ODATA_PATH}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            params={"$top": "2", "$format": "json"},
        )
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("d", {}).get("results", [])
            print(f"\nGot {len(results)} sales orders:")
            for r in results:
                print(json.dumps({
                    "SalesOrder": r.get("SalesOrder"),
                    "SoldToParty": r.get("SoldToParty"),
                    "TotalNetAmount": r.get("TotalNetAmount"),
                }, indent=2))
        else:
            print(f"Response: {resp.text[:500]}")


if __name__ == "__main__":
    main()
