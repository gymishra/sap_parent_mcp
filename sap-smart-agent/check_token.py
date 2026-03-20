"""Grab an Okta token using the bridge's auth flow and decode it."""
import os, sys, json, base64

# Reuse the bridge's token logic
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("OKTA_DOMAIN", "trial-1053860.okta.com")
os.environ.setdefault("OKTA_AUTH_SERVER", "default")
os.environ.setdefault("OKTA_CLIENT_ID", "0oa10vth79kZAuXGt698")
os.environ.setdefault("OKTA_CLIENT_SECRET", "0ALCNusqzJ9kT8aOKoFgItQ1u1CSIvPZ3caimB_TObB_E32c2WdP0ySh6l9MM7rW")
os.environ.setdefault("OKTA_SCOPES", "openid email")
os.environ.setdefault("OKTA_REDIRECT_URI", "http://localhost:8086/callback")

from kiro_bridge import get_okta_token

token = get_okta_token()
print("=== RAW TOKEN ===")
print(token)
print()

# Decode header and payload (no signature verification, just inspection)
parts = token.split(".")
for i, label in enumerate(["HEADER", "PAYLOAD"]):
    padded = parts[i] + "=" * (4 - len(parts[i]) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded))
    print(f"=== {label} ===")
    print(json.dumps(decoded, indent=2))
    print()
