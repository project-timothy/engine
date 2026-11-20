"""The receipt_extract job: per-receipt field proposals from the extractor,
for the review card to show (invariant 2: the model proposes, code decides).
"""

from __future__ import annotations

from pathlib import Path

from ...engine.contracts import EventSpec, JobContext, JobOutput
from ...engine.fileops import CannotVerify, CopyMismatch
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from ..ap.extraction import ExtractionError, build_extractor, resolved_tier
from ..ap.schema import amount_to_cents
from .common import (
    _file_copy,
    _filing_dir,
    _landed_by_sha,
    _placement_anomaly,
    _proposals_by_sha,
    _reference_shas,
    _tenant_month,
)
from .records import (
    PROPOSED_EVENT,
    REFERENCE_EVENT,
)
from .schema import (
    normalize_category,
    parse_amount_tag,
    parse_project_tag,
)

# ---- job: extract -----------------------------------------------------------


RECEIPT_EXTRACT_JOB = "receipt_extract"


def _extractor_kind(ctx: JobContext) -> str:
    import os

    return ctx.params.get("extractor") or os.environ.get("ENGINE_EXTRACTOR") or "claude"


def _extractor(ctx: JobContext):
    return build_extractor(_extractor_kind(ctx), ctx, job_type=RECEIPT_EXTRACT_JOB)


def _extract_pending(ctx: JobContext) -> list[dict]:
    landed = _landed_by_sha(ctx)
    proposed = set(_proposals_by_sha(ctx)) | _reference_shas(ctx)
    return [p for sha, p in sorted(landed.items()) if sha not in proposed]


def _extract_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expextract")
    key.value("pending", sorted(str(p.get("sha256")) for p in _extract_pending(ctx)))
    key.param("extractor")
    key.param("filing_dir")
    key.config("expenses")
    key.config("books.cost_object")  # the project tag in file names (#340)
    # Row 7.10: the receipt pass runs through the same gateway extractor, so
    # the resolved tier rides the key (docs/run-keys.md).
    key.config("llm")
    key.value("llm_tier", resolved_tier(ctx, _extractor_kind(ctx), RECEIPT_EXTRACT_JOB))
    return key.digest()


def _extract_run(ctx: JobContext) -> JobOutput:
    pending = _extract_pending(ctx)
    if not pending:
        return JobOutput(status="ok", summary="expenses extract: nothing pending")

    extractor = _extractor(ctx)
    filing = _filing_dir(ctx)
    events: list[EventSpec] = []
    actions: list[str] = []
    anomalies: list[Anomaly] = []
    proposed = referenced = failed = 0

    for payload in pending:
        sha = str(payload["sha256"])
        path = Path(str(payload.get("filed_to", "")))
        if not path.is_file():
            anomalies.append(
                Anomaly(
                    code="expenses.receipt_missing",
                    detail=f"{payload.get('file')} landed but is not at {path}",
                )
            )
            continue
        try:
            doc = extractor.extract(path)
        except ExtractionError as exc:
            failed += 1
            anomalies.append(Anomaly(code="expenses.extract_failed", detail=f"{path.name}: {exc}"))
            continue

        if doc.doc_type == "reference":
            month = str(payload.get("month") or _tenant_month(ctx))
            ref_dir = filing / month / "_reference"
            try:
                dest = _file_copy(path, ref_dir, guard=ctx.guard, shadow=ctx.shadow)
            except (CannotVerify, CopyMismatch) as exc:
                anomalies.append(_placement_anomaly("expenses", path.name, exc))
                continue  # the only copy stays put until the move is verified
            if ctx.shadow:
                # Nothing was written, so nothing is claimed (03-F8): a
                # recorded reference event would hide the receipt from every
                # later real run. Mirrors the intake loop's shadow guard.
                actions.append(f"would file {path.name} -> _reference/ ({ref_dir})")
                continue
            # The unlink stands on a read-back-verified copy (03-F4).
            ctx.guard.check_write(path)
            path.unlink()
            referenced += 1
            actions.append(f"reference material: {path.name} -> _reference/")
            events.append(
                EventSpec(
                    key=f"expense-ref:{sha}",
                    event_type=REFERENCE_EVENT,
                    payload={"sha256": sha, "file": path.name, "filed_to": str(dest)},
                )
            )
            continue

        flags: list[str] = []
        amount_cents: int | None = None
        if doc.amount is not None:
            amount_cents = amount_to_cents(doc.amount)
        tag_cents = parse_amount_tag(path.name)
        if tag_cents is not None and amount_cents is not None and tag_cents != amount_cents:
            flags.append(
                f"amount-in-filename ${tag_cents / 100:,.2f} disagrees with "
                f"proposal ${amount_cents / 100:,.2f}"
            )
        if amount_cents is None and tag_cents is not None:
            amount_cents = tag_cents
            flags.append("amount taken from filename; no amount extracted")
        if amount_cents is None:
            flags.append("no amount found; the review card must resolve this line")
        file_tag = parse_project_tag(path.name, ctx.tenant.books.cost_object)
        if file_tag and file_tag != parse_project_tag(
            str(payload.get("project", "")), ctx.tenant.books.cost_object
        ):
            flags.append(f"filename project tag {file_tag} vs attributed {payload.get('project')}")
        category = normalize_category(doc.category, path.name)

        proposed += 1
        actions.append(f"proposed {path.name}: {doc.vendor_name or '?'} {category or '?'}")
        events.append(
            EventSpec(
                key=f"expense-extract:{sha}",
                event_type=PROPOSED_EVENT,
                payload={
                    "sha256": sha,
                    "file": path.name,
                    "vendor": doc.vendor_name or "",
                    "expense_date": doc.invoice_date or "",
                    "amount_cents": amount_cents,
                    "category": category,
                    "confidence": doc.confidence,
                    "flags": flags,
                },
            )
        )

    return JobOutput(
        status="ok",
        summary=f"expenses extract: proposed {proposed}, reference {referenced}, failed {failed}",
        actions=actions,
        events=events,
        anomalies=anomalies,
    )
