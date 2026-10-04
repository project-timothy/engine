"""Microsoft Graph calendar writes: all-day deadline events on the owner's calendar.

Standard library HTTP, like graph_mail. The token comes from the same MSAL
cache in the keychain (graph_mail.keychain_token_provider), asked for the
delegated Calendars.ReadWrite scope alone, granted once with
`engine mail consent <tenant> --scope Calendars.ReadWrite`.

Create uses Graph's ``transactionId`` so a retried create after a lost reply
never makes a second event; a delete of an event the person already removed
(404) is treated as done.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from .graph_mail import GRAPH_BASE, GraphMailError

Transport = Callable[..., Any]
CATEGORY = "Deadlines"


class GraphCalendarError(GraphMailError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _default_transport(method: str, url: str, *, body: dict | None, headers: dict):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise GraphCalendarError(
            f"HTTP {exc.code} {method} {url.split('?')[0]}: {detail}", status=exc.code
        ) from exc


def _event(*, subject: str, day: date, body: str, reminder_minutes: int, time_zone: str) -> dict:
    return {
        "subject": subject,
        "body": {"contentType": "text", "content": body},
        "isAllDay": True,
        "start": {"dateTime": f"{day.isoformat()}T00:00:00", "timeZone": time_zone},
        "end": {
            "dateTime": f"{(day + timedelta(days=1)).isoformat()}T00:00:00",
            "timeZone": time_zone,
        },
        "isReminderOn": True,
        "reminderMinutesBeforeStart": int(reminder_minutes),
        "showAs": "free",
        "categories": [CATEGORY],
    }


class GraphCalendarClient:
    """Writes to the signed-in user's default calendar (/me/events)."""

    def __init__(self, *, token_provider: Callable[[], str], transport: Transport | None = None):
        self._token_provider = token_provider
        self._transport = transport or _default_transport
        self._token: str | None = None

    def _headers(self) -> dict:
        if self._token is None:
            self._token = self._token_provider()
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def create_all_day(
        self,
        *,
        subject: str,
        day: date,
        body: str,
        reminder_minutes: int,
        transaction_id: str,
        time_zone: str,
    ) -> str:
        payload = _event(
            subject=subject,
            day=day,
            body=body,
            reminder_minutes=reminder_minutes,
            time_zone=time_zone,
        )
        payload["transactionId"] = transaction_id
        made = self._transport(
            "POST", f"{GRAPH_BASE}/me/events", body=payload, headers=self._headers()
        )
        return str(made["id"])

    def update_all_day(
        self,
        event_id: str,
        *,
        subject: str,
        day: date,
        body: str,
        reminder_minutes: int,
        time_zone: str,
    ) -> None:
        payload = _event(
            subject=subject,
            day=day,
            body=body,
            reminder_minutes=reminder_minutes,
            time_zone=time_zone,
        )
        self._transport(
            "PATCH", f"{GRAPH_BASE}/me/events/{event_id}", body=payload, headers=self._headers()
        )

    def delete(self, event_id: str) -> None:
        try:
            self._transport(
                "DELETE", f"{GRAPH_BASE}/me/events/{event_id}", body=None, headers=self._headers()
            )
        except GraphCalendarError as exc:
            if exc.status != 404:  # already gone is done
                raise
