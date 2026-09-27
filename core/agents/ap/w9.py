"""W-9 intake routing (docs/w9-1099-design.md build 3, owner go 2026-09-01).

An inbound W-9 carries a TIN, and the design doc's invariant is absolute: no
TIN ever enters this repo, the ledger, a card, an event, or a log — the W-9
PDFs and the accounting system's vendor records are a TIN's only two homes.
So detection here is DETERMINISTIC and pre-model: filename tokens first (no
content read at all), then the PDF text layer's form markers, checked in
memory and discarded. A detected form never reaches the extractor model.

The flow (diff-emitting variant, the design doc's recommendation):

1. Intake detects the form, routes it (``ap.intake.routed.w9``), and parks
   ONE card proposing the registry flip. Vendor attribution is a PROPOSAL
   from the subject-alias match — card never guess: "unknown" asks the owner
   to supply ``--param vendor=`` at approval, and a wrong proposal is
   corrected the same way (issue #117 owner overrides).
2. On the approved card, the next intake run COPIES the archived original
   into the tenant's Vendor W-9s folder as ``<Vendor_Slug>_W9_<year>.<ext>``
   (the landing original is the audit artifact and never moves) and records
   ``ap.w9.filed`` carrying the registry diff as text. The engine never
   writes vendors.toml — the event is the diff's durable home and the PR
   flow picks it up from there.
3. Rejection means "not a W-9 / do not file": a final answer, so the stable
   content-keyed card correctly re-parks nothing (the #143 memory used
   right; contrast issue #161 where rejection meant re-ask).

Two rules the honesty audit (2026-09-03, 02-F4 and 02-F7) made explicit:

- Detection ALWAYS runs; ``[w9].folder`` gates FILING, not detection (the
  TIN invariant outranks configuration). With the folder unset a detected
  form is still routed (``ap.intake.routed.w9``, never the extractor) and
  named by ``ap.w9_folder_unset``, but NO card parks: a card that can never
  execute is the lie the audit named. A card approved while the folder was
  set and then orphaned by unsetting it is named by the same anomaly on
  every executed run, never left approved-and-silent.
- A failed detection is not a negative one. When a PDF candidate's text
  layer cannot be read (encrypted, malformed), the file is UNREADABLE, not
  "not a W-9": intake parks it in the review shape without any model call
  and records ``ap.w9_text_unreadable``. Sending it to the extractor on the
  strength of a failed read would break the TIN invariant.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...engine.contracts import ApprovalSpec, EventSpec, JobContext
from ...engine.fileops import CannotVerify, CopyMismatch, place_copy
from ...engine.guard import ProtectedSurfaceError
from ...engine.result import Anomaly
from .registry import VendorRegistry

W9_CARD = "ap.w9_file_and_flip"
W9_FILED_EVENT = "ap.w9.filed"

# detect() verdicts: a W-9 (route it), a PDF whose text layer could not be
# read (review it by hand, never model it), or None (an ordinary document).
DETECTED = "w9"
UNREADABLE = "unreadable"

# w9 / w-9 / fw9 as a token: not preceded or followed by another alphanumeric
# (BMW9000 and w94_report never match; "Alpha Parts W-9", "fw9.pdf" do).
_NAME_TOKEN = re.compile(r"(?<![a-z0-9])f?w-?9(?![a-z0-9])", re.IGNORECASE)

_TEXT_MARKERS = ("form w-9", "request for taxpayer")


class TextUnreadable(Exception):
    """The PDF text layer could not be read. Carries the parser's exception
    TYPE only, never its message or the document's text (02-F7)."""


def _read_text(path: Path) -> str:
    """The PDF text layer, read for the marker check only and discarded —
    never stored, logged, or propagated (the text contains the TIN).

    A parse failure raises :class:`TextUnreadable` instead of reading as
    "no markers": an unreadable candidate must never be classified "not a
    W-9" and handed to the extractor model (honesty audit 02-F7).
    """
    from .extraction import read_document_text

    try:
        return read_document_text(path)
    except Exception as exc:
        raise TextUnreadable(type(exc).__name__) from None


def _looks_like_w9_name(name: str) -> bool:
    return bool(_NAME_TOKEN.search(Path(name).stem))


def _looks_like_w9_text(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _TEXT_MARKERS)


def detect(path: Path) -> str | None:
    """Deterministic W-9 detection: filename first (no content read), then
    the PDF text markers. Returns :data:`DETECTED` for a form,
    :data:`UNREADABLE` for a PDF whose text layer raised (a failed check,
    not a negative one: the caller reviews it by hand and never models it),
    and ``None`` for an ordinary document. Image scans are caught by
    filename only — an image W-9 with a neutral filename falls through to
    the normal intake lanes (documented limitation; the extractor's
    doc_type router still files it as reference, never as an invoice)."""
    if _looks_like_w9_name(path.name):
        return DETECTED
    if path.suffix.lower() == ".pdf":
        try:
            text = _read_text(path)
        except TextUnreadable:
            return UNREADABLE
        return DETECTED if _looks_like_w9_text(text) else None
    return None


def _vendor_slug(vendor: str) -> str:
    """Display name -> the folder naming convention (SRR-style):
    alphanumerics kept, runs of everything else collapse to one underscore."""
    return re.sub(r"[^A-Za-z0-9]+", "_", vendor).strip("_")


def folder(ctx: JobContext) -> Path | None:
    """The Vendor W-9s folder: ``--param w9_folder`` override (tests), else
    ``[w9].folder``. Unset disables FILING, never detection (02-F4): a
    detected form is routed and named by ``ap.w9_folder_unset`` but no card
    parks, and an already-approved card is named by the same anomaly
    instead of executing."""
    raw = str(ctx.params.get("w9_folder") or ctx.tenant.w9.folder or "").strip()
    return Path(raw).expanduser() if raw else None


def route(
    ctx: JobContext, registry: VendorRegistry, path: Path, md5: str
) -> tuple[EventSpec, ApprovalSpec]:
    """The routed event + the one proposal card for a detected form."""
    from ...engine.clock import local_today

    entry = registry.resolve_subject(path.name)
    vendor = entry.vendor if entry else "unknown"
    tax_year = local_today(ctx.tenant.identity.timezone)[:4]
    event = EventSpec(
        key=f"route:{md5}",
        event_type="ap.intake.routed.w9",
        payload={"file": path.name, "doc_type": "w9", "vendor": vendor},
    )
    card = ApprovalSpec(
        key=f"w9:{md5}",
        action_type=W9_CARD,
        params={
            "file": path.name,
            "md5": md5,
            "vendor": vendor,
            "tax_year": tax_year,
            "proposed": "w9 = true",
        },
        reason=(
            f"inbound W-9 detected ({path.name}); proposed vendor: {vendor}. "
            "Read the form, then approve with --param classification=<as the "
            "form reads: individual/sole-prop, s-corp, c-corp, llc-disregarded, "
            "partnership> and, if the vendor is unknown or wrong, --param "
            "vendor=<canonical registry name>. On approval the next intake run "
            "copies the form to the Vendor W-9s folder and emits the registry "
            "diff (the engine never writes vendors.toml). Reject = not a W-9 / "
            "do not file. The engine never reads Part I: classification and "
            "TIN handling stay the owner's acts."
        ),
    )
    return event, card


def _find_source(landing: Path, name: str, md5: str, md5_fn) -> Path | None:
    """The archived original: landing root first, then the janitor's
    ``_archive/YYYY-MM`` subfolders. Content-verified by hash, never by
    name alone."""
    candidates = [landing / name]
    candidates.extend(sorted(landing.glob(f"_archive/*/{name}")))
    for candidate in candidates:
        if candidate.is_file() and md5_fn(candidate) == md5:
            return candidate
    return None


def execute_approved(
    ctx: JobContext,
    landing: Path,
    approved_cards: list[dict],
    filed_md5s: set[str],
    md5_fn,
) -> tuple[list[EventSpec], list[str], list[Anomaly]]:
    """Copy each approved form into the W-9 folder and emit the diff event.

    The event is the execution memory (re-runs no-op); a failed copy (cloud
    folder offline/evicted) records an anomaly and NO event, so the next run
    retries. A write-guard refusal is NOT a retryable copy failure: it
    propagates as a job error (02-F8). Identical content already at the
    destination is adopted; a true collision files under a numbered suffix
    (the filing rule). With the folder unset, every unexecuted approved card
    is named by ``ap.w9_folder_unset`` (02-F4), nothing else happens.
    """
    events: list[EventSpec] = []
    actions: list[str] = []
    anomalies: list[Anomaly] = []
    dest_root = folder(ctx)

    for card in approved_cards:
        params = card["params"]
        md5 = str(params.get("md5", ""))
        if not md5 or md5 in filed_md5s:
            continue
        if dest_root is None:
            anomalies.append(
                Anomaly(
                    code="ap.w9_folder_unset",
                    detail=f"approved W-9 card {card.get('id')} ({params.get('file')}) "
                    "cannot execute: [w9].folder is unset; set the folder (inside a "
                    "write-guard carve-out) and the next intake run files it",
                )
            )
            continue
        vendor = str(params.get("vendor", "")).strip()
        if not vendor or vendor == "unknown":
            anomalies.append(
                Anomaly(
                    code="ap.w9_vendor_missing",
                    detail=f"approved W-9 card for {params.get('file')} has no vendor; "
                    "re-approve with --param vendor=<canonical registry name>",
                )
            )
            continue
        name = str(params.get("file", ""))
        source = _find_source(landing, name, md5, md5_fn)
        if source is None:
            anomalies.append(
                Anomaly(
                    code="ap.w9_source_missing",
                    detail=f"approved W-9 {name} not found in the landing tree "
                    "(hash mismatch or file removed); locate and file by hand",
                )
            )
            continue
        year = str(params.get("tax_year", "")) or "undated"
        classification = str(params.get("classification", "")).strip()
        dest = dest_root / f"{_vendor_slug(vendor)}_W9_{year}{source.suffix.lower()}"
        # Honesty audit 2026-09-03 (02-F5): the shared filing rule. Identical
        # content is adopted at any suffix, different content moves to the
        # next _n as many times as it takes, nothing is ever overwritten, and
        # a placeholder on either side is "cannot verify" (deferred), never
        # read as a collision. The copy is read back before it is called filed.
        try:
            placed = place_copy(
                source,
                dest,
                guard=ctx.guard,
                shadow=ctx.shadow,
                hash_fn=md5_fn,
                suffix_fmt="{stem}_{n}{suffix}",
            )
        except CannotVerify as exc:
            anomalies.append(
                Anomaly(
                    code="ap.w9_copy_deferred",
                    detail=f"{name} -> {dest}: cannot verify the {exc.side} "
                    "(cloud-only placeholder); deferred, retried next run",
                )
            )
            continue
        except CopyMismatch as exc:
            anomalies.append(
                Anomaly(
                    code="ap.w9_copy_unverified",
                    detail=f"{name} -> {exc.dest}: the copy read back different content; "
                    "removed, retried next run",
                )
            )
            continue
        except ProtectedSurfaceError:
            # A guard refusal never clears on a retry: the folder sits outside
            # the write-guard carve-out. That is a job error, not a
            # "cloud folder offline" (02-F8).
            raise
        except OSError as exc:
            anomalies.append(
                Anomaly(
                    code="ap.w9_copy_failed",
                    detail=f"{name} -> {dest}: {exc}; will retry next run "
                    "(cloud folder offline or evicted?)",
                )
            )
            continue
        dest = placed.dest
        if ctx.shadow:
            actions.append(f"would file {name} -> {dest}")
            continue
        diff = (
            f"vendors.toml — {vendor}: w9 = true (signed form filed {dest}); "
            f"tax_classification = {classification or 'unset (owner reads it off the form)'}"
        )
        actions.append(f"filed W-9 {name} -> {dest}")
        events.append(
            EventSpec(
                key=f"w9filed:{md5}",
                event_type=W9_FILED_EVENT,
                payload={
                    "md5": md5,
                    "file": name,
                    "dest": str(dest),
                    "vendor": vendor,
                    "tax_year": year,
                    "classification": classification,
                    "diff": diff,
                },
            )
        )
    return events, actions, anomalies
