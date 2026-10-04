"""Print the QBO consent URL for the engine's production app.
Reads client_id from ~/.qbo-tokens.json; client IDs are public, secrets never print."""

import json
import os
import sys
import urllib.parse

t = json.load(open(os.path.expanduser("~/.qbo-tokens.json")))
cid = t["client_id"]
if "PASTE" in cid.upper():
    sys.exit("client_id is still a placeholder; fill ~/.qbo-tokens.json first")

params = {
    "client_id": cid,
    "response_type": "code",
    "scope": "com.intuit.quickbooks.accounting",
    "redirect_uri": "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl",
    "state": "engine-prod",
}
print("https://appcenter.intuit.com/connect/oauth2?" + urllib.parse.urlencode(params))
