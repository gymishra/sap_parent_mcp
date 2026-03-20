"""Quick check: get Okta token and print its claims. No SAP or AgentCore calls."""
import os, json, base64, webbrowser, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import httpx

OKTA_DOMAIN = "trial-1053860.okta.com"
OKTA_AUTHORIZE_URL = f"https://{OKTA_DOMAIN}/oauth2/v1/authorize"
OKTA_TOKEN_URL = f"https://{OKTA_DOMAIN}/oauth2/v1/token"
OKTA_CLIENT_ID = "0oa10vth79kZAuXGt698"
OKTA_CLIENT_SECRET = os.environ.get("OKTA_CLIENT_SECRET", "")
OKTA_REDIRECT_URI = "http://localhost:8085/callback"

class H(BaseHTTPRequestHandler):
    code = None
    def do_GET(self, *a, **k):
        p = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in p:
            H.code = p["code"][0]
            self.send_response(200); self.end_headers()
            self.wfile.write(b"Done, return to terminal")
        else:
            self.send_response(400); self.end_headers()
    def log_message(self, *a): pass

def decode_jwt(token, label):
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.b64decode(payload))
        print(f"\n--- {label} ---")
        for k in ["iss", "aud", "cid", "sub", "scp", "exp"]:
            if k in claims:
                print(f"  {k}: {claims[k]}")
    except Exception as e:
        print(f"  Could not decode {label}: {e}")

url = f"{OKTA_AUTHORIZE_URL}?{urllib.parse.urlencode({'client_id': OKTA_CLIENT_ID, 'response_type': 'code', 'scope': 'openid email', 'redirect_uri': OKTA_REDIRECT_URI, 'state': 'abc'})}"
srv = HTTPServer(("localhost", 8085), H)
Thread(target=srv.handle_request, daemon=True).start()
print("Opening browser..."); webbrowser.open(url)
srv.handle_request(); srv.server_close()

creds = base64.b64encode(f"{OKTA_CLIENT_ID}:{OKTA_CLIENT_SECRET}".encode()).decode()
resp = httpx.post(OKTA_TOKEN_URL, data={"grant_type": "authorization_code", "code": H.code, "redirect_uri": OKTA_REDIRECT_URI}, headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"})
resp.raise_for_status()
data = resp.json()

print(f"\nToken response keys: {list(data.keys())}")
if "access_token" in data: decode_jwt(data["access_token"], "ACCESS TOKEN")
if "id_token" in data: decode_jwt(data["id_token"], "ID TOKEN")

print(f"\n\nAgentCore discovery URL should match the ACCESS TOKEN 'iss' value + /.well-known/openid-configuration")
