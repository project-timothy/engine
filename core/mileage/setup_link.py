"""The phone's tracker app, set up without typing (chunk 10; read from
Traccar Client 10.2's source, one Flutter codebase for iPhone and Android).

On the phone, a tap on ``app_link`` opens Traccar Client, which asks the
person to confirm, then takes the box as its server, the person's tracker
token as its id, and ``TRACKER_SETTINGS``; ``START_LINK`` then turns it on
with no second question. On a computer, ``qr_text`` is scanned from the
app's own settings (its QR button), which reads the same settings and takes
the address's origin and path as the server. Either way the app posts each
position as a form (id, lat, lon, timestamp) to ``/track`` on the box
(core/mileage/door.py).

``TRACKER_SETTINGS`` follows the owner's call of 2026-10-15: Highest
accuracy (GPS on, interval and angle active), offline buffering, stop
detection. Traccar gives no iOS numbers, so ``distance`` (meters while
moving), ``interval`` (seconds) and ``angle`` (degrees) are starting values
until the pocketed-phone drive test against a trip odometer sets them.

Traccar's maintainer confirmed the link and its parameter names are stable
(docs/decisions/2026-10-22-setup-by-saying-yes.md, Checked against the source).
"""

from __future__ import annotations

from urllib.parse import urlencode

TRACK_PATH = "/track"  # where the box takes positions (core/tools/mcp_http.py)

TRACKER_SETTINGS = {
    "accuracy": "highest",
    "distance": "50",
    "interval": "60",
    "angle": "30",
    "buffer": "true",
    "stop_detection": "true",
}

START_LINK = "org.traccar.client://action/start"

STORES = {
    "iphone": "https://apps.apple.com/app/traccar-client/id843156974",
    "android": "https://play.google.com/store/apps/details?id=org.traccar.client",
}


def app_link(origin: str, token: str) -> str:
    """The tap that sets the app up on the phone itself."""
    query = urlencode({"url": origin + TRACK_PATH, "id": token, **TRACKER_SETTINGS})
    return f"org.traccar.client://?{query}"


def qr_text(origin: str, token: str) -> str:
    """What the QR holds, for a person setting up from a computer."""
    return f"{origin}{TRACK_PATH}?{urlencode({'id': token, **TRACKER_SETTINGS})}"


def qr_rows(text: str) -> list[str]:
    """``text`` as a QR code: one string of 1s (dark) and 0s per row, for a
    page to draw. Medium error correction, so a phone screen's glare reads."""
    import segno

    made = segno.make(text, error="m", micro=False)
    return ["".join("1" if cell else "0" for cell in row) for row in made.matrix]


__all__ = ["START_LINK", "STORES", "TRACKER_SETTINGS", "app_link", "qr_rows", "qr_text"]
