"""A person's own calendar feed (chunk 11): the dates Tim watches that they
may see, as a calendar their phone subscribes to.

The setup step's yes makes a standing link, kept like the receipt page's
(core/engine/keyfile.py: shown once, only its hash on file, one per
person). ``/cal/<key>.ics`` answers with the same open dates the deadlines
tool gives that person (core/tools/catalog.open_deadlines), each with its
reminders ahead, in the calendar file's own form (core/agents/deadlines/
ics.py). Tim only adds dates; it never reads the calendar. The first fetch
by a calendar, not a browser opening the link, tells setup the step works.
Standard library only, but for the QR code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from ..agents.deadlines.ics import CalendarEntry, render_calendar
from ..engine.keyfile import add_key, drop_keys, person_for_key
from ..mileage.setup_link import qr_rows
from .catalog import Tools, open_deadlines

CAL_PATH = "/cal/"
LINKS_FILE = "calendar_links.toml"
HORIZON_DAYS = 400  # a year ahead and a little: next year's renewal is on the calendar
GOOGLE_ADD = "https://calendar.google.com/calendar/render?cid="
_HEADER = "Calendar feed links' SHA-256 -> person (core/tools/calendar_feed.py)."
_ROUTE = re.compile(r"^/cal/([A-Za-z0-9_-]{32})\.ics$")


def add_calendar_link(directory: Path, person: str) -> str:
    """A standing feed key for ``person``: shown once, kept only as its
    hash; their older one stops working."""
    return add_key(Path(directory) / LINKS_FILE, person, header=_HEADER, only_one=True)


def drop_calendar_link(directory: Path, person: str) -> int:
    """Retire ``person``'s feed link; how many there were."""
    return drop_keys(Path(directory) / LINKS_FILE, person, header=_HEADER)


def feed_person(directory: Path, path: str) -> str | None:
    """Whose feed ``path`` is, or None for anything else."""
    found = _ROUTE.match(path)
    return person_for_key(Path(directory) / LINKS_FILE, found.group(1)) if found else None


def links(origin: str, key: str) -> dict[str, Any]:
    """What the setup screen hands over: the feed, the one-tap subscribe
    (iPhone, Mac, Outlook), Google's add-by-link (Android, any computer),
    and a QR of the feed for a phone across the table."""
    feed = f"{origin}{CAL_PATH}{key}.ics"
    subscribe = "webcal://" + urlsplit(feed).netloc + urlsplit(feed).path
    return {
        "feed": feed,
        "subscribe": subscribe,
        "google": GOOGLE_ADD + quote(subscribe, safe=""),
        "qr": qr_rows(feed),
    }


def by_a_calendar(headers: Any) -> bool:
    """A calendar fetching the feed, not a browser opening the link."""
    accept = str(headers.get("Accept") or "")
    return headers.get("Sec-Fetch-Mode") != "navigate" and "text/html" not in accept


def render(tools: Tools, ministry: str) -> str:
    """The person's open dates as a calendar file."""
    entries = [
        CalendarEntry(
            uid=f"{ob.id}-{due:%Y%m%d}@timothy",
            due=due,
            summary=ob.title,
            description=ob.notes,
            leads=tuple(leads),
        )
        for ob, due, _left, leads in open_deadlines(tools, HORIZON_DAYS)
    ]
    name = f"Tim: {ministry}" if ministry else "Tim"
    return render_calendar(entries, name=name, stamp=tools._today())


__all__ = [
    "CAL_PATH",
    "LINKS_FILE",
    "add_calendar_link",
    "by_a_calendar",
    "drop_calendar_link",
    "feed_person",
    "links",
    "render",
]
