"""Every web call the engine makes goes through here (the security review
prep of 2026-10-13). The rule is code, not a comment: https to anywhere, or
plain http only to this machine (a local model gateway, a test's fake
server). Anything else, ``file:`` included, is refused before a socket opens,
so a URL that arrives through config or a reply can never read a local file
or talk in the clear across a network.

The auditor keeps its own copy (auditor/web.py): it imports nothing from
core/, by design."""

from __future__ import annotations

import urllib.request
from typing import Any
from urllib.parse import urlsplit

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class SchemeRefused(ValueError):
    """A web call to anything but https, or plain http beyond this machine."""


def checked(url: str) -> str:
    """``url`` itself when the engine may call it; otherwise SchemeRefused."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return url
    where = parts.hostname or url[:60]
    raise SchemeRefused(
        f"refused a {parts.scheme or 'schemeless'} web call to {where}: "
        "https only, plain http only to this machine"
    )


def request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
) -> urllib.request.Request:
    """A request the engine may send (the scheme checked first)."""
    return urllib.request.Request(  # noqa: S310  (scheme checked on the line above)
        checked(url), data=data, headers=headers or {}, method=method
    )


def urlopen(req: urllib.request.Request | str, *, timeout: float) -> Any:
    """``urllib.request.urlopen`` behind the same check."""
    checked(req.full_url if isinstance(req, urllib.request.Request) else req)
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310  (scheme checked above)
