"""Filesystem sweep: the landing tree vs the engine ledger (read-only).

The inverse of the parity diff. The diff compares the engine ledger against the
legacy ledger and catches disagreements; the sweep compares the filesystem
against the engine ledger and catches an invoice that physically exists but the
engine never processed, because it was filed where intake does not look (a
subfolder, the manual ``_skipped`` pile). 2026-06-24 shadow finding: a real D&E
payable sat unbooked in ``_skipped``, invisible to the date-windowed,
ledger-vs-ledger diff. The sweep is the net under that gap.

Read-only: it walks files and reads the ledger; it writes nothing and moves
nothing. The verdict is the count of invoice-like files the engine has never
seen; pure graphics are counted but never raise the verdict.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ...engine.guard import WriteGuard
from ...ledger import Ledger
from .schema import AP_CANDIDATE_SUFFIXES

# An event idempotency key is "<prefix>:<md5>" for every file the engine
# touches (flag, route, new, file, ocr, vendor, incomplete, revised); only
# skip uses a name. A trailing 32-hex run is therefore the file's fingerprint.
_MD5_IN_KEY = re.compile(r":([0-9a-f]{32})$")
_INVOICE_WORD = re.compile(r"invoice|\binv\b", re.IGNORECASE)
_DIGIT_RUN = re.compile(r"\d{3,}")
# A subfolder whose name marks it as a deliberate non-invoice pile: the operator
# has already triaged its contents, so the sweep honors that label and does not
# re-flag them. _skipped itself carries no such marker, so it is still scanned
# (that is where a misfiled invoice hides).
_NON_INVOICE_DIR = re.compile(r"non.?invoice|misrouted", re.IGNORECASE)


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _is_invoice_like(name: str, suffix: str) -> bool:
    """A name that reads like an invoice: the word "invoice"/"inv", or a PDF
    carrying an invoice-number-length digit run. Pure graphics (logos,
    screenshots) named ``imageNNN.png`` do not qualify, so the sweep stays quiet
    on the junk pile while still surfacing a real invoice misfiled there.
    """
    if _INVOICE_WORD.search(name):
        return True
    return suffix == ".pdf" and bool(_DIGIT_RUN.search(name))


def _md5s_from_events(events: Iterable[dict]) -> set[str]:
    found: set[str] = set()
    for event in events:
        match = _MD5_IN_KEY.search(str(event.get("idempotency_key", "")))
        if match:
            found.add(match.group(1))
    return found


def seen_fingerprints(ledger: Ledger, tenant: str) -> set[str]:
    """Every file fingerprint the engine has already processed: the recorded
    invoices' ``source_md5`` plus every md5 embedded in an event key.
    """
    seen: set[str] = set()
    for row in ledger.conn.execute(
        "SELECT source_md5 FROM ap_invoices WHERE tenant = ? AND source_md5 != ''",
        (tenant,),
    ):
        seen.add(row[0])
    seen |= _md5s_from_events(ledger.read_event_log())
    return seen


@dataclass
class SweepReport:
    scanned: int = 0
    unbooked_invoices: list[str] = field(default_factory=list)
    other_unbooked: int = 0

    @property
    def is_clean(self) -> bool:
        return not self.unbooked_invoices


def collect_unbooked(landing_dir: str | Path, seen: set[str]) -> SweepReport:
    """Surface invoice-like files in the landing tree's SUBFOLDERS whose
    fingerprint the engine has never seen. ``seen`` is the fingerprint set.

    The top level is intake's own windowed domain, so its pre-window backlog
    (hundreds of older files the engine never fingerprinted) is not a sweep
    finding and would drown the signal. The sweep covers only the subfolders
    intake structurally never enters (``_skipped`` and any others), which is the
    permanent blind spot where a misfiled invoice hides (docs/lessons.md,
    "The sweep watches where intake never looks").
    """
    report = SweepReport()
    root = Path(landing_dir)
    if not root.is_dir():
        return report
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.parent == root:
            continue  # top level is intake's job; the sweep covers subfolders
        rel = path.relative_to(root)
        if any(_NON_INVOICE_DIR.search(part) for part in rel.parts[:-1]):
            continue  # operator-marked non-invoice folder; honor the label
        if path.suffix.lower() not in AP_CANDIDATE_SUFFIXES:
            continue
        if path.name.endswith(".extract.json"):
            continue  # fixture sidecars are metadata, not candidates
        report.scanned += 1
        if _md5(path) in seen:
            continue  # the engine has already processed this exact file
        if _is_invoice_like(path.name, path.suffix.lower()):
            report.unbooked_invoices.append(rel.as_posix())
        else:
            report.other_unbooked += 1
    return report


def sweep(landing_dir: str | Path, ledger: Ledger, tenant: str) -> SweepReport:
    return collect_unbooked(landing_dir, seen_fingerprints(ledger, tenant))


def render_markdown(report: SweepReport, *, tenant: str) -> str:
    verdict = "CLEAN" if report.is_clean else "NEEDS ATTENTION"
    lines = [
        f"# Filesystem sweep: {tenant} / ap",
        "",
        f"Generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        f"## Verdict: {verdict}",
        "",
        f"- candidate files scanned: {report.scanned}",
        f"- unbooked invoice-like files: {len(report.unbooked_invoices)}",
        f"- other unbooked files (not invoice-like): {report.other_unbooked}",
    ]
    if report.unbooked_invoices:
        lines += [
            "",
            "## Unbooked invoice-like files (the engine has no record of these)",
            "",
            "Each looks like an invoice, but its fingerprint is absent from the",
            "ledger, so intake never processed it (most likely filed in a",
            "subfolder the scan does not enter, e.g. _skipped). Confirm each: if",
            "it is a real payable the engine should see, move it to the landing",
            "folder top level so the next intake picks it up.",
            "",
        ]
        lines += [f"- {item}" for item in report.unbooked_invoices]
    if report.is_clean:
        lines += ["", "Every invoice-like file in the tree is accounted for."]
    lines.append("")
    return "\n".join(lines)


def run_sweep(
    *,
    tenant_slug: str,
    ledger: Ledger,
    landing_dir: str | Path,
    guard: WriteGuard,
    out_path: str | Path | None = None,
) -> tuple[SweepReport, Path | None]:
    """Produce the sweep report; optionally write it inside the ledger root."""
    report = sweep(landing_dir, ledger, tenant_slug)
    written: Path | None = None
    if out_path is not None:
        target = guard.check_write(out_path)  # never a protected surface
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_markdown(report, tenant=tenant_slug), encoding="utf-8")
        written = target
    return report, written
