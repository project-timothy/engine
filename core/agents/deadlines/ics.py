"""The calendar file: open obligations as RFC 5545 all-day events.

Any calendar app reads it (Apple Calendar, Outlook, Google), and each event
carries one alarm per lead window, so the phone says "passport renewal in 90
days" without the engine sending anything. A generated view, like the AP
workbook: rewritten whole on every live scan, never hand-edited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

PRODID = "-//Project Timothy//engine deadlines//EN"


@dataclass(frozen=True)
class CalendarEntry:
    uid: str
    due: date
    summary: str
    description: str
    leads: tuple[int, ...]


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Split a content line at 75 octets (continuations start with a space),
    never inside a UTF-8 character."""
    out: list[str] = []
    current, size, limit = "", 0, 75
    for ch in line:
        width = len(ch.encode("utf-8"))
        if size + width > limit:
            out.append(current)
            current, size, limit = " ", 1, 75
        current += ch
        size += width
    out.append(current)
    return out


def _trigger(lead: int) -> str:
    return "TRIGGER:PT0S" if lead == 0 else f"TRIGGER:-P{lead}D"


def render_calendar(entries: list[CalendarEntry], *, name: str, stamp: date) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(name)}",
    ]
    for e in sorted(entries, key=lambda x: (x.due, x.uid)):
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e.uid}",
            f"DTSTAMP:{stamp:%Y%m%d}T000000Z",
            f"DTSTART;VALUE=DATE:{e.due:%Y%m%d}",
            f"DTEND;VALUE=DATE:{e.due + timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{_escape(e.summary)}",
        ]
        if e.description:
            lines.append(f"DESCRIPTION:{_escape(e.description)}")
        lines.append("TRANSP:TRANSPARENT")
        for lead in e.leads:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_escape(e.summary)}",
                _trigger(lead),
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    folded = [part for line in lines for part in _fold(line)]
    return "\r\n".join(folded) + "\r\n"
