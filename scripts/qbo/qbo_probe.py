"""Validate QBO credentials: refresh-token -> access token -> CompanyInfo.
Persists the rotated refresh token back (Intuit rotates on every refresh)."""

import base64
import json
import os
import sys
import urllib.request

p = os.path.expanduser("~/.qbo-tokens.json")
t = json.load(open(p))
if "PASTE_" in "".join(str(v) for v in t.values()):
    sys.exit("tokens file still has placeholders; fill ~/.qbo-tokens.json first")

auth = base64.b64encode(f"{t['client_id']}:{t['client_secret']}".encode()).decode()
req = urllib.request.Request(
    "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
    data=f"grant_type=refresh_token&refresh_token={t['refresh_token']}".encode(),
    headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    },
)
tok = json.load(urllib.request.urlopen(req))
t["refresh_token"] = tok["refresh_token"]  # rotated; persist the new one
json.dump(t, open(p, "w"), indent=2)

req = urllib.request.Request(
    f"https://quickbooks.api.intuit.com/v3/company/{t['realm_id']}/companyinfo/{t['realm_id']}?minorversion=73",
    headers={"Authorization": f"Bearer {tok['access_token']}", "Accept": "application/json"},
)
info = json.load(urllib.request.urlopen(req))["CompanyInfo"]
print(f"CONNECTED: {info['CompanyName']} (realm {t['realm_id']})")
