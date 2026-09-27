"""Exchange a QBO authorization code for tokens and persist them.

Usage: python3 qbo_exchange_code.py <code> <realmId>

Reads client_id/client_secret from ~/.qbo-tokens.json, writes refresh_token
and realm_id back into the same file. Prints only success/failure, never a
token value: the file is the sole home for secrets.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

if len(sys.argv) != 3:
    sys.exit("usage: qbo_exchange_code.py <code> <realmId>")
code, realm = sys.argv[1], sys.argv[2]

p = os.path.expanduser("~/.qbo-tokens.json")
t = json.load(open(p))
if "PASTE" in (t["client_id"] + t["client_secret"]).upper():
    sys.exit("client_id/client_secret still placeholders; fill ~/.qbo-tokens.json first")

auth = base64.b64encode(f"{t['client_id']}:{t['client_secret']}".encode()).decode()
body = (
    "grant_type=authorization_code"
    f"&code={urllib.parse.quote(code)}"
    "&redirect_uri="
    + urllib.parse.quote("https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl", safe="")
)
req = urllib.request.Request(
    "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
    data=body.encode(),
    headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    },
)
try:
    tok = json.load(urllib.request.urlopen(req))
except urllib.error.HTTPError as e:
    sys.exit(f"exchange FAILED: HTTP {e.code}: {e.read().decode()[:300]}")

t["refresh_token"] = tok["refresh_token"]
t["realm_id"] = realm
json.dump(t, open(p, "w"), indent=2)
os.chmod(p, 0o600)
print(f"exchange OK: refresh token stored, realm {realm}. Run qbo_probe.py to verify.")
