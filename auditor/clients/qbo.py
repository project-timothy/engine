"""The auditor's own QBO fetch: queries only, nothing else.

A deliberate duplicate of the minimum the QBO-consistency lens needs. It
shares exactly two things with the engine, both by convention rather than
import: the token FILE (Intuit rotates the refresh token on every use, so
whoever exchanges must persist atomically) and the advisory lock beside it
(the design's one QBO coordination point — the auditor runs at 02:00, six
hours from the engine's 08:00, and the lock makes contention safe anyway).

Read-only is structural: the only POST in this module is the OAuth token
exchange. There is no create, update, or delete call to duplicate — and
the independence lint keeps the engine's write code un-importable.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
API_BASE = "https://quickbooks.api.intuit.com/v3/company"
# Intuit retired minor versions 1-74 on 2025-08-01; every request below 75 is served as 75 since.
MINOR_VERSION = "75"
LOCK_FILENAME = ".qbo-tokens.lock"  # the engine's lock name, honored by convention
CHUNK = 40  # ids per IN(...) query, well inside QBO's query-length limits
PAGE = 1000  # QBO's MAXRESULTS ceiling; larger lists page by STARTPOSITION

Transport = Callable[..., dict]


class AuditorQboError(RuntimeError):
    pass


def cents(total_amt: object) -> int:
    """Dollars-and-cents to integer cents through Decimal, never float math."""
    return int((Decimal(str(total_amt)) * 100).to_integral_value())


def _default_transport(url: str, *, data: bytes | None, headers: dict) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise AuditorQboError(f"HTTP {exc.code} from {url.split('?')[0]}: {detail}") from exc


class AuditorQboClient:
    def __init__(self, token_file: str | Path, *, transport: Transport | None = None) -> None:
        self._path = Path(token_file).expanduser()
        self._transport = transport or _default_transport
        self._access_token: str | None = None
        self._realm: str | None = None

    def _refresh(self) -> None:
        """Exchange the refresh token under the shared advisory lock and
        persist the ROTATED one atomically before releasing it."""
        if not self._path.exists():
            raise AuditorQboError(f"no QBO token file at {self._path}")
        with open(self._path.parent / LOCK_FILENAME, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            tokens = json.loads(self._path.read_text())
            from base64 import b64encode

            auth = b64encode(f"{tokens['client_id']}:{tokens['client_secret']}".encode()).decode()
            body = urllib.parse.urlencode(
                {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
            ).encode()
            response = self._transport(
                TOKEN_URL,
                data=body,
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
            tokens["refresh_token"] = response["refresh_token"]
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".qbo-tokens-")
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(tokens, handle, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        self._access_token = response["access_token"]
        self._realm = str(tokens["realm_id"])

    def _query(self, query: str) -> dict:
        if self._access_token is None:
            self._refresh()
        url = f"{API_BASE}/{self._realm}/query?" + urllib.parse.urlencode(
            {"query": query, "minorversion": MINOR_VERSION}
        )
        return self._transport(
            url,
            data=None,
            headers={"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"},
        )

    def fetch_vendors(self) -> list[dict]:
        """Every vendor's id, display name, active flag, and Vendor1099 flag.
        Pages past QBO's 1000-row cap with STARTPOSITION (the #145 lesson).
        The Vendor query returns no TaxIdentifier field (probed 2026-08-26),
        so nothing TIN-shaped is ever requested or held here."""
        out: list[dict] = []
        start = 1
        while True:
            response = self._query(
                "SELECT Id, DisplayName, Active, Vendor1099 FROM Vendor "
                f"STARTPOSITION {start} MAXRESULTS {PAGE}"
            )
            batch = (response.get("QueryResponse") or {}).get("Vendor", [])
            out.extend(
                {
                    "id": str(v.get("Id", "")),
                    "display_name": str(v.get("DisplayName", "")),
                    "active": bool(v.get("Active", True)),
                    "vendor_1099": bool(v.get("Vendor1099", False)),
                }
                for v in batch
            )
            if len(batch) < PAGE:
                return out
            start += PAGE

    def fetch_by_ids(self, entity: str, ids: list[str]) -> dict[str, int]:
        """{id: amount_cents} for every id of ``entity`` QBO still holds.
        An id absent from the result no longer exists there."""
        found: dict[str, int] = {}
        unique = sorted({str(i) for i in ids if str(i)})
        for start in range(0, len(unique), CHUNK):
            chunk = unique[start : start + CHUNK]
            listed = ",".join(f"'{i}'" for i in chunk)
            response = self._query(
                f"SELECT Id, TotalAmt FROM {entity} WHERE Id IN ({listed}) MAXRESULTS 1000"
            )
            for obj in (response.get("QueryResponse") or {}).get(entity, []):
                found[str(obj.get("Id", ""))] = cents(obj.get("TotalAmt", 0))
        return found
