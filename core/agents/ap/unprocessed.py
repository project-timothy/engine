"""Unprocessed intake: files the engine saw but could not turn into an invoice.

Four kinds, all recorded as events keyed by the file's md5: extraction failures
(`ap.intake.flagged`), image-only files (`ap.intake.needs_ocr`), unknown-vendor
documents (`ap.intake.unknown_vendor`, 2026-07-10: previously these had no
disposition path and re-flagged forever), and incomplete extractions
(`ap.intake.incomplete`). They surface
in the workbook's "needs identification" section until the owner resolves each:

- dismiss it (not an invoice; emits `ap.intake.dismissed`), or
- identify it (a missed invoice; manual entry records an ap_invoices row whose
  source_md5 is the file's, so it drops out of here).

Once resolved either way it never resurfaces: a resolved md5 is filtered out
(2026-06-26 design). Resolution is by command, never by editing the workbook
(which is a regenerated view).
"""

from __future__ import annotations

import re

from ...ledger import Ledger

_MD5_IN_KEY = re.compile(r":([0-9a-f]{32})$")
_UNPROCESSED_EVENTS = {
    "ap.intake.flagged",
    "ap.intake.needs_ocr",
    "ap.intake.unknown_vendor",
    "ap.intake.incomplete",
}
_REASON = {
    "ap.intake.flagged": "extraction failed",
    "ap.intake.needs_ocr": "image-only, needs OCR",
    "ap.intake.unknown_vendor": "unknown vendor",
    "ap.intake.incomplete": "extraction incomplete",
}


def _md5_of_key(key: str) -> str | None:
    match = _MD5_IN_KEY.search(str(key))
    return match.group(1) if match else None


def _dismissed_md5s(ledger: Ledger) -> set[str]:
    return {
        _md5_of_key(e.get("idempotency_key", ""))
        for e in ledger.read_event_log()
        if e.get("event_type") == "ap.intake.dismissed"
    } - {None}


def _recorded_md5s(ledger: Ledger, tenant: str) -> set[str]:
    rows = ledger.conn.execute(
        "SELECT source_md5 FROM ap_invoices WHERE tenant = ? AND source_md5 != ''",
        (tenant,),
    )
    return {r[0] for r in rows}


def unresolved_unprocessed(ledger: Ledger, tenant: str) -> list[dict]:
    """Unprocessed files not yet dismissed or identified (resolved md5s drop)."""
    resolved = _dismissed_md5s(ledger) | _recorded_md5s(ledger, tenant)
    by_md5: dict[str, dict] = {}
    for event in ledger.read_event_log():
        event_type = event.get("event_type")
        if event_type not in _UNPROCESSED_EVENTS:
            continue
        md5 = _md5_of_key(event.get("idempotency_key", ""))
        if not md5 or md5 in resolved:
            continue
        by_md5[md5] = {
            "md5": md5,
            "file": event.get("payload", {}).get("file", ""),
            "reason": _REASON.get(event_type, "needs identification"),
        }
    return sorted(by_md5.values(), key=lambda item: item["file"])


def md5_for_file(ledger: Ledger, file: str) -> str | None:
    """The md5 of an unprocessed file, by filename (for dismiss / identify).

    Returns the most recent matching event's md5, or None if the file is not a
    known unprocessed item.
    """
    found: str | None = None
    for event in ledger.read_event_log():
        if event.get("event_type") not in _UNPROCESSED_EVENTS:
            continue
        if event.get("payload", {}).get("file") == file:
            found = _md5_of_key(event.get("idempotency_key", "")) or found
    return found
