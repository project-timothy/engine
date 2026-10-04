"""Combined-scan splitting for the expenses agent intake (issue #104).

The first real receipt drop (2026-08-10) was ONE 17-page PDF: a batch scan
of 17 receipts. Extract proposes one line per file, so without a splitter a
17-receipt scan becomes a single flagged proposal. Batch-scan-one-PDF is how
office users naturally feed the tree; per-receipt files is our convention,
not theirs.

Division of labor (invariant 2):

- **Detector (code):** any receipt PDF with more than one page is a
  suspected combined scan. Unreadable PDFs count as one page — a corrupt
  file is extraction's problem, not the splitter's.
- **Grouping proposal (LLM):** a grouper proposes page groups, one group per
  receipt, plus vendor/amount/date used only to name the child files. A
  multi-page SINGLE receipt (the hotel-folio shape) comes back as one group
  and the file is left whole.
- **Split (code):** coverage is validated in code — every page exactly once,
  or the scan is held with an anomaly and nothing is filed. pypdf writes the
  children; the LLM never touches file contents.

The proposed vendor/amount/date land only in child FILENAMES, where the
extract stage's deterministic cross-checks treat them as claims to verify,
never as accepted values.

The grouping call runs through the model seam since phase 7 row 7.11
(:class:`GatewayGrouper`): which model answers is ``[llm.tiers]`` plus
``[llm.jobs].scan_group``, every call is priced, budget-checked, and recorded
in ``llm_calls``, and the transport settings belong to the adapter the tier
names rather than to this module. What did NOT move is the division of labor
above: ``_parse_groups_payload`` and ``validate_groups`` are still the code
that decides, and a scan the model cannot group is held, never filed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from core.llm import Attachment, Message

from .schema import parse_project_tag

_TIMEOUT_S = 120
MAX_SPLIT_FILE_BYTES = 40 * 1024 * 1024

SCAN_GROUP_JOB = "scan_group"
"""The policy key for the combined-scan split pass (``[llm.jobs].scan_group``)."""

SITE = "grouper"
"""What an unknown ``--param grouper=`` value is called in the error."""

# The domain brief, shared by every tier. The reply SHAPE is the gateway's
# job (it appends the JSON schema and pydantic validates what comes back);
# COVERAGE is this module's job, in code, after the reply is parsed.
_GROUP_SPEC = (
    "You are grouping the pages of a scanned receipt batch. The scan may "
    "contain one receipt or several receipts scanned together. Group the "
    "pages so each group is exactly one receipt (a multi-page receipt like a "
    "hotel folio is ONE group). Every page must appear in exactly one group. "
    "pages are 1-indexed page numbers; vendor, amount (e.g. 12.34), and date "
    "(YYYY-MM-DD) name the child file and may be empty when you cannot read "
    "them."
)

_GROUP_ASK = "Group the pages of the attached {pages}-page scan, one group per receipt."


class GroupingError(RuntimeError):
    """The grouper could not produce a usable proposal."""


@dataclass
class ScanGroup:
    pages: list[int]
    vendor: str = ""
    amount: str = ""
    date: str = ""


@dataclass
class SplitChild:
    path: Path
    pages: list[int] = field(default_factory=list)


class Grouper(Protocol):
    def propose_groups(self, path: Path, page_count: int) -> list[ScanGroup]: ...


def pdf_page_count(path: Path) -> int:
    """Pages in a PDF, or 1 when unreadable (a corrupt file goes through the
    normal single-file flow, where extraction fails loudly per file)."""
    try:
        from pypdf import PdfReader

        from core.engine.timebox import pdf_deadline

        with pdf_deadline(Path(path).name):
            return len(PdfReader(str(path)).pages)
    except Exception:
        return 1


def validate_groups(groups: list[ScanGroup], page_count: int) -> str:
    """'' when every page appears exactly once, else the human reason."""
    if not groups:
        return "the grouper proposed no groups"
    seen: list[int] = []
    for g in groups:
        if not g.pages:
            return "a proposed group holds no pages"
        seen.extend(g.pages)
    if sorted(seen) != list(range(1, page_count + 1)):
        return (
            f"proposed groups cover pages {sorted(set(seen))} of a "
            f"{page_count}-page scan; every page must appear exactly once"
        )
    return ""


# ---- child naming ------------------------------------------------------------


def _slug(text: str, limit: int = 24) -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "", text.strip())
    return out[:limit]


def _amount_token(raw: str) -> str:
    """'108.37' -> '$108.37' when parseable, else ''. The token must match
    schema._AMOUNT_TAG so extract's filename cross-check can read it."""
    text = raw.strip().lstrip("$").replace(",", "")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        return ""
    cents = round(float(text) * 100)
    return f"${cents // 100}.{cents % 100:02d}"


def normalized_stem(stem: str) -> str:
    """The original's stem with any project tag folded to underscore form,
    so children carry attribution intake can parse the same way."""
    tag = parse_project_tag(stem)
    if tag and "-" in tag:
        return stem.replace(tag, tag.replace("-", "_"))
    return stem


def child_name(stem: str, index: int, group: ScanGroup) -> str:
    # Space-joined, matching the live filename style ("dinner $43.87.pdf").
    # An underscore here would glue onto a trailing project tag and break
    # schema's \b-anchored tag parse (P26_2034_r01 carries no boundary).
    parts = [normalized_stem(stem), f"r{index:02d}"]
    if group.vendor:
        parts.append(_slug(group.vendor))
    if group.date.strip():
        parts.append(group.date.strip())
    amount = _amount_token(group.amount)
    if amount:
        parts.append(amount)
    name = " ".join(p for p in parts if p)
    return f"{name}.pdf"


# ---- the split itself (code, never the LLM) ----------------------------------


def split_pdf(path: Path, groups: list[ScanGroup]) -> list[SplitChild]:
    """Write one child PDF per group next to the original. Returns the
    children; the caller owns eventing and archiving the original."""
    from pypdf import PdfReader, PdfWriter

    from core.engine.timebox import pdf_deadline

    with pdf_deadline(Path(path).name):
        reader = PdfReader(str(path))
        pages = list(reader.pages)
    children: list[SplitChild] = []
    for index, group in enumerate(groups, start=1):
        writer = PdfWriter()
        for page_no in group.pages:
            writer.add_page(pages[page_no - 1])
        target = path.parent / child_name(path.stem, index, group)
        n = 2
        while target.exists():
            target = target.with_name(f"{target.stem} ({n}){target.suffix}")
            n += 1
        with target.open("wb") as fh:
            writer.write(fh)
        children.append(SplitChild(path=target, pages=list(group.pages)))
    return children


# ---- groupers -----------------------------------------------------------------


def _parse_groups_payload(payload: object, source: str) -> list[ScanGroup]:
    if not isinstance(payload, list):
        raise GroupingError(f"{source}: expected a JSON array of groups")
    groups: list[ScanGroup] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("pages"), list):
            raise GroupingError(f"{source}: each group needs a 'pages' list")
        try:
            pages = [int(p) for p in item["pages"]]
        except (TypeError, ValueError) as exc:
            raise GroupingError(f"{source}: page numbers must be integers") from exc
        groups.append(
            ScanGroup(
                pages=pages,
                vendor=str(item.get("vendor") or ""),
                amount=str(item.get("amount") or ""),
                date=str(item.get("date") or ""),
            )
        )
    return groups


class FixtureGrouper:
    """Reads a ``<file>.groups.json`` sidecar (tests/CI), the same pattern as
    ap.extraction.FixtureExtractor."""

    def propose_groups(self, path: Path, page_count: int) -> list[ScanGroup]:
        sidecar = path.with_name(path.name + ".groups.json")
        if not sidecar.is_file():
            raise GroupingError(f"no groups sidecar for {path.name}")
        return _parse_groups_payload(json.loads(sidecar.read_text()), sidecar.name)


class GroupReply(BaseModel):
    """One proposed group as the model returns it. Every field is a CLAIM:
    the pages are checked for coverage in code, and vendor/amount/date only
    ever reach a child filename."""

    pages: list[int] = Field(default_factory=list)
    vendor: str = ""
    amount: str = ""  # dollars as text, never arithmetic input
    date: str = ""


class GroupingReply(BaseModel):
    """The gateway's output contract for a grouping call.

    The natural reply here is a LIST, and the seam's top-level output is
    always an object, so the list rides inside one
    (``docs/model-seam-design.md``). The groups still come back through
    :func:`_parse_groups_payload`, which is the code that owns the parse."""

    groups: list[GroupReply] = Field(default_factory=list)


class GatewayGrouper:
    """Live grouping through the model seam.

    One call per scan: ``complete_for(ctx, job_type, ...)`` resolves the tier
    from the tenant policy, prices it, refuses it past the monthly budget,
    records it in ``llm_calls``, and validates the reply. Everything
    provider-specific belongs to the adapter the tier names.

    Failure policy, unchanged in intent from the pre-gateway grouper: anything
    about THIS SCAN (a transport blip, a reply the engine cannot use, a spent
    budget) becomes a :class:`GroupingError`, so the scan is held with an
    anomaly and nothing is filed; anything about the DEPLOYMENT (a missing
    ``[claude]`` extra, a policy gap) propagates and fails the job loudly (the
    #172 failure trace) rather than holding every scan in the tree.

    ``tier`` pins a tier by name (``--param grouper=tier:<name>``);
    ``adapter`` overrides the wire for every attempt (tests and evals hand in
    a seeded fixture adapter); ``completer`` is injected so a test can run
    with no policy at all.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        job_type: str = SCAN_GROUP_JOB,
        tier: str | None = None,
        timeout_s: int = _TIMEOUT_S,
        max_file_bytes: int = MAX_SPLIT_FILE_BYTES,
        adapter: Any | None = None,
        completer: Any | None = None,
    ) -> None:
        self._ctx = ctx
        self._job_type = job_type
        self._tier = tier
        self._timeout_s = timeout_s
        self._max_file_bytes = max_file_bytes
        self._adapter = adapter
        self._complete = completer

    def propose_groups(self, path: Path, page_count: int) -> list[ScanGroup]:
        from core.llm.policy import complete_for

        size = path.stat().st_size
        if size > self._max_file_bytes:
            raise GroupingError(
                f"{path.name} is {size} bytes, over the {self._max_file_bytes}-byte "
                "split cap; split it manually"
            )
        messages = [
            Message("system", _GROUP_SPEC),
            Message("user", _GROUP_ASK.format(pages=page_count)),
        ]
        call = self._complete or complete_for
        try:
            result = call(
                self._ctx,
                self._job_type,
                messages,
                GroupingReply,
                attachments=[Attachment(path, "application/pdf")],
                timeout_s=self._timeout_s,
                adapter=self._adapter,
                tier=self._tier,
            )
        except Exception as exc:
            raise self._as_grouping_error(path, exc) from exc
        payload = [group.model_dump() for group in result.output.groups]
        return _parse_groups_payload(payload, path.name)

    def _as_grouping_error(self, path: Path, exc: Exception) -> Exception:
        """Per-scan failures become a GroupingError (this file is held);
        deployment and configuration failures are returned unchanged so they
        escape the split pass entirely."""
        from core.llm import GatewayTransportError, GatewayValidationError
        from core.llm.policy import BudgetExceeded, LlmPolicyError

        if isinstance(exc, GatewayTransportError):
            if exc.cause == "sdk_missing":
                return exc  # the [claude] extra: a deployment fault
            if exc.cause == "timeout":
                return GroupingError(f"grouping timed out for {path.name} after {self._timeout_s}s")
            return GroupingError(f"grouping failed for {path.name}: {exc}")
        if isinstance(exc, GatewayValidationError):
            return GroupingError(f"unusable grouping reply for {path.name}")
        if isinstance(exc, BudgetExceeded):
            # The month's cap: this scan waits, the rest of intake runs, and
            # the runner still surfaces the refusal as an llm.budget anomaly.
            return GroupingError(f"grouping failed for {path.name}: {exc}")
        if isinstance(exc, LlmPolicyError | GroupingError):
            return exc
        return GroupingError(f"grouping failed for {path.name}: {exc}")


def resolved_tier(ctx: Any, kind: str, job_type: str = SCAN_GROUP_JOB) -> tuple | None:
    """``(tier, adapter, model)`` for what this grouper name would call, or
    ``None`` for the fixture grouper. The intake run key folds it in, so the
    key moves when the tier does."""
    from core.llm.policy import resolved_tier_for_alias

    return resolved_tier_for_alias(ctx.tenant.llm, kind, job_type, site=SITE)


def build_grouper(kind: str, ctx: Any | None = None, *, job_type: str = SCAN_GROUP_JOB) -> Grouper:
    """Resolve a grouper by name.

    ``fixture`` is the sidecar grouper (tests, and the only name that needs no
    tenant policy). ``claude`` is the tier the policy names for this job and
    stays an alias for one release; ``tier:<name>`` says it directly. Anything
    else raises naming what is legal.
    """
    from core.llm.policy import FIXTURE_ALIAS, tier_for_alias

    if kind == FIXTURE_ALIAS:
        return FixtureGrouper()
    if ctx is None:
        raise ValueError(
            f"{SITE} {kind!r} calls a model, so it needs the job's tenant context; "
            f"only {FIXTURE_ALIAS!r} runs without one"
        )
    tier = tier_for_alias(ctx.tenant.llm, kind, site=SITE)
    return GatewayGrouper(ctx, job_type=job_type, tier=tier)
