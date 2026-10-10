"""AP agent jobs: intake, verify-payment, queue-status, apply, workbook.

The LLM boundary runs through ``extraction``: classification and field
extraction may come from a model, validated by pydantic before use; vendor
matching, dedup, amount arithmetic, ledger writes, and payment verification
are deterministic code (invariant 2). Every action that would move money or
touch a production surface lands in the approval queue instead of executing
(invariant 7).

``apply`` is the execution step: it files every recorded invoice not yet
filed by COPYING the landing original into the filing tree (never a move; the
original is the audit artifact). No approval gate: a successfully-extracted
invoice is real (junk is kept out upstream by the pre-filter and the
unprocessed pile), and filing is not a money move (invariant 7 protects
payments/sends, which the engine never does on its own). It is shadow-aware (a
shadow run is a dry-run that writes nothing) and guard-safe (the destination
passes the write guard, so a filing_dir inside a protected surface is refused
even live). It touches no money and no external accounting system.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from decimal import Decimal
from pathlib import Path

from ...authority import CardRule
from ...engine.config import MissingFolderError
from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobHandler, JobOutput
from ...engine.fileops import CannotVerify, CopyMismatch, place_copy, strip_collision_suffix
from ...engine.guard import ProtectedSurfaceError, WriteGuard
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from ..audit.bank_recon import orphan_checks, status_lag_anomalies
from ..timesheets.schema import is_timesheet_name as _is_timesheet_name
from . import provenance, qbo_push, qbo_push_payments, store, w9
from .extraction import (
    INVOICE_EXTRACT_JOB,
    ExtractionError,
    Extractor,
    build_extractor,
    resolved_tier,
)
from .inputs import retry_day as _retry_day
from .inputs import vendors as _vendors
from .qbo_push import earliest_iso_date as earliest_iso_date
from .registry import VendorRegistry
from .rubric import score_intake
from .schema import AP_CANDIDATE_SUFFIXES, BillpayEntry, ExtractedDocument, amount_to_cents
from .verify import classify_payable

EXTRACTOR_ENV = "ENGINE_EXTRACTOR"


# ---------- shared helpers -------------------------------------------------


def _landing_dir(ctx: JobContext) -> Path:
    override = ctx.params.get("landing_dir")
    configured = override or ctx.tenant.ap.landing_dir
    if not configured:
        raise ValueError(
            "no landing directory: set [ap].landing_dir in tenant.toml or pass "
            "--param landing_dir=PATH"
        )
    return Path(configured)


def _extractor_kind(ctx: JobContext) -> str:
    """The extractor name for this run: ``--param extractor=`` wins, then the
    environment override, then ``claude`` (which since row 7.10 means "the
    tier the tenant policy names for this job")."""
    return ctx.params.get("extractor") or os.environ.get(EXTRACTOR_ENV) or "claude"


def _extractor(ctx: JobContext) -> Extractor:
    return build_extractor(_extractor_kind(ctx), ctx, job_type=INVOICE_EXTRACT_JOB)


def _candidates(ctx: JobContext) -> list[Path]:
    landing = _landing_dir(ctx)
    if not landing.is_dir():
        raise MissingFolderError(f"landing directory {landing} does not exist")
    since = ctx.params.get("since")  # ISO date; optional
    out: list[Path] = []
    for path in sorted(landing.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name.endswith(".extract.json"):
            continue  # fixture sidecars are metadata, not candidates
        if since:
            from datetime import date, datetime
            from zoneinfo import ZoneInfo

            # Midnight on the TENANT's wall clock (#136): a UTC midnight
            # cutoff excludes files landed in the tenant's late evening.
            cutoff = datetime.combine(
                date.fromisoformat(since),
                datetime.min.time(),
                tzinfo=ZoneInfo(ctx.tenant.identity.timezone),
            ).timestamp()
            if path.stat().st_mtime < cutoff:
                continue
        out.append(path)
    return out


def _file_bytes(path: Path) -> bytes:
    """Raw read, split out so evals can simulate a cloud-only placeholder."""
    return path.read_bytes()


def _md5(path: Path) -> str | None:
    """Hex MD5 of the file, or ``None`` for a cloud-only placeholder.

    The landing folder can live on a cloud-sync drive with on-demand files:
    a placeholder with no local content fails ``read()`` with
    ``OSError(EDEADLK)`` in a headless
    session (launchd), where no GUI File Provider context hydrates it on
    demand. One such file must not kill the run (docs/lessons.md,
    "A cloud placeholder is not here yet"), so that specific errno means
    "not here yet" and the caller defers the file. Every other ``OSError`` still propagates.
    """
    try:
        data = _file_bytes(path)
    except OSError as exc:
        if exc.errno == errno.EDEADLK:
            return None
        raise
    return hashlib.md5(data).hexdigest()


# Inline email graphics saved into the landing folder alongside real
# attachments: Outlook names inline images "Outlook-<token>.png" and email
# bodies carry "imageNNN.png" (or a bare "image.png" for the first one).
# Image-only and name-shaped, so a real invoice PDF (or a real scanned-image
# invoice) never matches (docs/lessons.md, "Code filters what code can decide").
_INLINE_GRAPHIC = re.compile(r"^(image\d*|Outlook-[\w-]+)\.(png|jpe?g|gif|bmp)$", re.IGNORECASE)


def _looks_like_inline_graphic(name: str) -> bool:
    """The rule reads the document's name, not the destination's.

    Every email body reuses the same handful of inline-image names, so the
    second copy of one logo lands under the placement contract's collision
    suffix (``core.engine.fileops``, "image001 (2).png"). That suffix is the
    folder's doing, so it comes off before the name-shaped rule is applied;
    otherwise byte-shaped junk the rule was written for walks past it into a
    model call, and one logo gets two different dispositions on two nights.
    """
    stem = Path(name).stem
    return bool(_INLINE_GRAPHIC.match(strip_collision_suffix(stem) + Path(name).suffix))


# ---------- intake ----------------------------------------------------------


def _intake_key(ctx: JobContext) -> str:
    """Same landing-folder state (names + content) = same run = no-op.

    A cloud-only placeholder folds in as a stable ``name:cloud-only``
    sentinel: the key stays put while the file has no local content, and
    changes the moment it materializes (the hash replaces the sentinel), so
    the next run re-fires and the deferred file is processed. Deferral is
    self-healing, never a silent drop.

    Vendor identity and the extractor choice are run inputs too (issue
    #135): onboarding a vendor must re-fire intake on the same landing
    state instead of replaying the FLAGGED result — the 2026-07-20 lesson
    the verify and push keys already carry. The digest covers every field
    resolution reads: canonical names, ledger aliases, subject aliases.
    """
    key = RunKey(ctx, "intake")
    key.param("since")
    key.param("extractor")
    key.env(EXTRACTOR_ENV)
    # Row 7.10: the extractor NAME is an alias onto a tier, so the name alone
    # is not the input. The resolved tier, its adapter, and its model id are:
    # repointing [llm.jobs].invoice_extract at another tier must re-extract,
    # not replay a result the previous model produced.
    key.config("llm")
    key.value("llm_tier", resolved_tier(ctx, _extractor_kind(ctx), INVOICE_EXTRACT_JOB))
    key.vendors(_vendors(ctx))
    key.config("ap")
    # W-9 lane (build 3): the folder is an input, and so is the approved-card
    # set — approving a W-9 card must re-fire intake on the same landing
    # state so the copy executes (the qbo-push key's rule).
    key.param("w9_folder")
    key.config("w9")
    key.config("identity")  # tz names the W-9 tax year (clock rule, #147)
    key.value("w9_exec", sorted(c["id"] for c in _approved_w9_cards(ctx)))
    candidates = _candidates(ctx)
    digests = {path: _md5(path) for path in candidates}
    key.files(candidates, digest=digests.__getitem__)
    # A transport flag is a verdict about the pipe, not the document
    # (docs/lessons.md, "Transient is not terminal"): nothing above changes when the transport
    # recovers, so the flagged file replayed as a noop forever. While a
    # retryable flag names a file still in the landing set, the tenant-local
    # day is an input (the expenses match verify-pass shape, #171): the flag
    # landing re-fires the next run once, further same-day re-runs replay,
    # and each later day's run extracts the file again.
    retrying = sorted(_retryable_flagged_md5s(ctx) & {d for d in digests.values() if d})
    key.value("retry_flagged", retrying)
    if retrying:
        key.value("retry_day", _retry_day(ctx))
    return key.digest()


# Flag causes that say nothing about the document: the model or CLI was
# unreachable (``transport_error``) or did not answer in time (``timeout``).
# ``oversize`` and ``bad_reply`` are verdicts about the file and stay final.
_RETRYABLE_FLAG_CAUSES = frozenset({"transport_error", "timeout"})


# Cards no agent may decide (#356 check 2): the queue CLI refuses them unless
# a person at a terminal types the card number back, and the auditor flags any
# such card resolved without that. The stamp in the card's params is what the
# auditor reads; the CLI gates by action type, so older cards are covered too.
NEW_VENDOR_CARD = "ap.new_vendor_decision"
HUMAN_ONLY = frozenset({NEW_VENDOR_CARD})


def _provenance_event(
    ctx: JobContext,
    mail_index: tuple[dict, dict],
    path: Path,
    name: str,
    md5: str,
    vendor: str,
    registry: VendorRegistry,
) -> EventSpec:
    by_hash, by_name = mail_index
    origin = by_hash.get(hashlib.sha256(path.read_bytes()).hexdigest()) or by_name.get(name)
    policy = ctx.tenant.ap.provenance
    verdict = provenance.judge(
        origin,
        vendor,
        registry,
        internal_domains=policy.internal_sender_domains,
        platform_domains=policy.platform_sender_domains,
        freemail_domains=policy.freemail_domains,
    )
    return EventSpec(
        key=f"prov:{md5}",
        event_type=provenance.PROVENANCE_EVENT,
        payload={
            "file": name,
            "vendor": vendor,
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "sender": (origin.sender or None) if origin else None,
            "sender_domain": origin.domain if origin else None,
        },
    )


def _retryable_flagged_md5s(ctx: JobContext) -> set[str]:
    """md5s whose LATEST intake disposition is a retryable flag.

    Every per-document disposition event ends its key with the file's md5
    (``unprocessed._MD5_IN_KEY``), so the newest ``ap.intake.*`` /
    ``ap.invoice.*`` event per md5 is the file's current state: a later
    record, route, needs_ocr, dismiss, identify, or a re-flag with a final
    cause all take the file out of the retry set.
    """
    from .unprocessed import _md5_of_key

    latest: dict[str, dict] = {}
    for event in ctx.ledger.read_event_log():
        event_type = str(event.get("event_type", ""))
        if not event_type.startswith(("ap.intake.", "ap.invoice.")):
            continue
        md5 = _md5_of_key(event.get("idempotency_key", ""))
        if md5:
            latest[md5] = event
    return {
        md5
        for md5, event in latest.items()
        if event.get("event_type") == "ap.intake.flagged"
        and (event.get("payload") or {}).get("cause") in _RETRYABLE_FLAG_CAUSES
    }


def _approved_w9_cards(ctx: JobContext) -> list[dict]:
    rows = ctx.ledger.conn.execute(
        "SELECT id, params_json FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? AND status = 'approved'",
        (ctx.tenant_slug, w9.W9_CARD),
    ).fetchall()
    return [{"id": int(r["id"]), "params": json.loads(r["params_json"])} for r in rows]


def _w9_filed_md5s(ctx: JobContext) -> set[str]:
    """Execution memory: md5s already copied + diff-emitted (event is truth)."""
    return {
        str(e["payload"].get("md5", ""))
        for e in ctx.ledger.read_event_log()
        if e["event_type"] == w9.W9_FILED_EVENT
    }


def _classify_invoice(
    ctx: JobContext, doc: ExtractedDocument, vendor: str, md5: str
) -> tuple[str, int | None]:
    """NEW / DUPLICATE / ATTACH / REVISED against the engine's own rows.

    Identity is vendor + invoice number; amount differences mean REVISED,
    never a silent overwrite (the legacy EOD-review discipline carries over).
    """
    existing = store.find_invoice(ctx.ledger, ctx.tenant_slug, vendor, doc.invoice_number or "")
    if existing is None:
        return "NEW", None
    if doc.amount is not None and amount_to_cents(doc.amount) != existing["amount_cents"]:
        return "REVISED", existing["id"]
    if existing["source_md5"] == md5 or existing["source_file"]:
        return "DUPLICATE", existing["id"]
    # ATTACH: the identity exists but was recorded without a source file (e.g. a
    # manual entry that carried no PDF), so a later physical document attaches to
    # that row rather than double-booking it. Intake always sets a source_file,
    # so this is a defensive branch for non-intake insert paths.
    return "ATTACH", existing["id"]


def _intake_run(ctx: JobContext) -> JobOutput:
    extractor = _extractor(ctx)
    registry = _vendors(ctx)

    counts = {
        "NEW": 0, "DUPLICATE": 0, "ATTACH": 0, "REVISED": 0,
        "FLAGGED": 0, "ROUTED": 0, "SKIPPED": 0, "NEEDS_OCR": 0, "DEFERRED": 0,
    }  # fmt: skip
    events: list[EventSpec] = []
    approvals: list[ApprovalSpec] = []
    anomalies: list[Anomaly] = []
    actions: list[str] = []
    validation_failures = 0
    extracted_count = 0

    # -- execute approved W-9 cards first (build 3): copy to the W-9 folder
    # and emit the registry diff; the filed event is the memory, a failed
    # cloud copy records an anomaly and retries next run. These events
    # carry no disposition count (the rubric below excludes them, 02-F9).
    w9_events, w9_actions, w9_anomalies = w9.execute_approved(
        ctx, _landing_dir(ctx), _approved_w9_cards(ctx), _w9_filed_md5s(ctx), _md5
    )
    events.extend(w9_events)
    actions.extend(w9_actions)
    anomalies.extend(w9_anomalies)
    # Detection ALWAYS runs (the TIN invariant outranks configuration); the
    # folder gates FILING only (02-F4): unset means a detected form is routed
    # and named by an anomaly, but no card parks, because a card that can
    # never execute is a lie.
    w9_root = w9.folder(ctx)
    # Retryable-flagged files are extracted again below as if new (no skip
    # rule ever excluded them; the key is what used to replay the run).
    retrying = _retryable_flagged_md5s(ctx)
    # Who mailed each landed file (#356): the mail fetch's saved records,
    # indexed once per run by content hash and by name.
    mail_index = provenance.mail_origins(ctx.ledger.read_event_log())
    provenance_count = 0

    for path in _candidates(ctx):
        name = path.name
        if path.suffix.lower() not in AP_CANDIDATE_SUFFIXES:
            counts["SKIPPED"] += 1
            events.append(
                EventSpec(
                    key=f"skip:{name}",
                    event_type="ap.intake.skipped",
                    payload={"file": name, "reason": "non-candidate suffix"},
                )
            )
            continue

        if _looks_like_inline_graphic(name):
            # Deterministic pre-filter: an inline email graphic is never an
            # invoice, so skip it before the model call instead of letting it
            # fail extraction or stall in the needs_ocr queue (docs/lessons.md,
            # "Code filters what code can decide"). Code decides, not the model (invariant 2).
            counts["SKIPPED"] += 1
            events.append(
                EventSpec(
                    key=f"skip:{name}",
                    event_type="ap.intake.skipped",
                    payload={"file": name, "reason": "inline email graphic, not an invoice"},
                )
            )
            continue

        md5 = _md5(path)
        if md5 is None:
            # Cloud-sync placeholder with no local content: extraction would
            # fail the same read, so defer before it. Visible three ways
            # (count, event, anomaly) and self-healing via the intake-key
            # sentinel; the operator fix is pinning the landing folder
            # (docs/lessons.md, "A cloud placeholder is not here yet").
            counts["DEFERRED"] += 1
            anomalies.append(
                Anomaly(
                    code="ap.cloud_only_deferred",
                    detail=f"{name}: cloud-only in the landing folder; deferred until materialized",
                )
            )
            events.append(
                EventSpec(
                    key=f"defer:{name}",
                    event_type="ap.intake.deferred",
                    payload={
                        "file": name,
                        "reason": "cloud-only file; content not materialized locally",
                    },
                )
            )
            continue
        verdict = w9.detect(path)
        if verdict == w9.DETECTED:
            # Deterministic pre-model gate (build 3): a W-9 carries a TIN, so
            # it must never reach the extractor model. Route + park the one
            # proposal card; code decides, not the model (invariant 2, the
            # inline-graphic precedent).
            counts["ROUTED"] += 1
            w9_event, w9_card = w9.route(ctx, registry, path, md5)
            events.append(w9_event)
            if w9_root is None:
                anomalies.append(
                    Anomaly(
                        code="ap.w9_folder_unset",
                        detail=f"detected W-9 {name}: routed but not carded, [w9].folder "
                        "is unset; configure [w9].folder to file it",
                    )
                )
                continue
            approvals.append(w9_card)
            continue
        if verdict == w9.UNREADABLE:
            # The text layer raised, so the W-9 check did not run: a failed
            # detection is not a negative one, and the file must not reach
            # the extractor on the strength of it (the TIN invariant, 02-F7).
            # Park it in the manual-review shape with no model call.
            counts["NEEDS_OCR"] += 1
            anomalies.append(
                Anomaly(
                    code="ap.w9_text_unreadable",
                    detail=f"{name}: PDF text layer unreadable, so the W-9 check could "
                    "not run; parked for manual review, never sent to the extractor",
                )
            )
            events.append(
                EventSpec(
                    key=f"ocr:{md5}",
                    event_type="ap.intake.needs_ocr",
                    payload={"file": name, "reason": "text layer unreadable; W-9 check skipped"},
                )
            )
            approvals.append(
                ApprovalSpec(
                    key=f"ocr:{md5}",
                    action_type="ap.review_needs_ocr",
                    params={"file": name},
                    reason="PDF text layer unreadable (W-9 check could not run); "
                    "open it by hand: a W-9 files through the W-9 lane, anything "
                    "else gets OCR or manual review",
                )
            )
            continue

        extracted_count += 1
        try:
            doc = extractor.extract(path)
        except ExtractionError as exc:
            validation_failures += 1
            counts["FLAGGED"] += 1
            anomalies.append(Anomaly(code="ap.extraction_failed", detail=f"{name}: {exc}"))
            # A repeat failure on a retry day is its own attempt, so its key
            # names the day: the runner already prefixes the run key (so the
            # ledger key would differ regardless), but the suffix makes the
            # event stream read as attempt-by-day rather than a duplicate,
            # and the md5 stays LAST so the unprocessed pile, dismiss, and
            # identify keep finding the file (``unprocessed._MD5_IN_KEY``).
            flag_key = f"flag:{_retry_day(ctx)}:{md5}" if md5 in retrying else f"flag:{md5}"
            events.append(
                EventSpec(
                    key=flag_key,
                    event_type="ap.intake.flagged",
                    # cause makes the drop diagnosable from the ledger alone
                    # (timeout vs transport vs oversize vs bad_reply) instead of
                    # a generic reason (docs/lessons.md, "Transient is not terminal"). Live
                    # extraction has already retried transient causes before
                    # reaching here.
                    payload={
                        "file": name,
                        "reason": "extraction failed",
                        "cause": exc.cause,
                    },
                )
            )
            continue

        if doc.needs_ocr:
            counts["NEEDS_OCR"] += 1
            events.append(
                EventSpec(
                    key=f"ocr:{md5}",
                    event_type="ap.intake.needs_ocr",
                    payload={"file": name},
                )
            )
            approvals.append(
                ApprovalSpec(
                    key=f"ocr:{md5}",
                    action_type="ap.review_needs_ocr",
                    params={"file": name},
                    reason="image-only document; OCR or manual review",
                )
            )
            continue

        if doc.doc_type != "invoice":
            counts["ROUTED"] += 1
            events.append(
                EventSpec(
                    key=f"route:{md5}",
                    event_type=f"ap.intake.routed.{doc.doc_type}",
                    payload={
                        "file": name,
                        "doc_type": doc.doc_type,
                        "vendor": doc.vendor_name,
                        "confidence": doc.confidence,
                    },
                )
            )
            continue

        # Vendor resolution: extracted name, then subject-alias style match on
        # the filename. Unknown vendor is an escalation, never an auto-onboard.
        entry = None
        if doc.vendor_name:
            entry = registry.resolve_name(doc.vendor_name) or registry.resolve_subject(
                doc.vendor_name
            )
        if entry is None:
            entry = registry.resolve_subject(name)
        if entry is None:
            counts["FLAGGED"] += 1
            events.append(
                EventSpec(
                    key=f"vendor:{md5}",
                    event_type="ap.intake.unknown_vendor",
                    payload={"file": name, "extracted_vendor": doc.vendor_name},
                )
            )
            approvals.append(
                ApprovalSpec(
                    key=f"vendor:{md5}",
                    action_type=NEW_VENDOR_CARD,
                    params={
                        "file": name,
                        "extracted_vendor": doc.vendor_name or "",
                        "human_only": "true",
                    },
                    reason="unknown vendor; onboarding is an owner decision",
                )
            )
            continue

        # Sender provenance (#356, shadow): did the mail that delivered this
        # file come from the vendor it names? A verdict event only; nothing
        # below reads it, so no invoice records differently because of it.
        events.append(_provenance_event(ctx, mail_index, path, name, md5, entry.vendor, registry))
        provenance_count += 1

        if doc.invoice_number is None or doc.amount is None:
            counts["FLAGGED"] += 1
            events.append(
                EventSpec(
                    key=f"incomplete:{md5}",
                    event_type="ap.intake.incomplete",
                    payload={
                        "file": name,
                        "vendor": entry.vendor,
                        "invoice_number": doc.invoice_number,
                        "amount": str(doc.amount) if doc.amount is not None else None,
                    },
                )
            )
            approvals.append(
                ApprovalSpec(
                    key=f"incomplete:{md5}",
                    action_type="ap.review_incomplete_extraction",
                    params={"file": name, "vendor": entry.vendor},
                    reason="invoice missing number or amount after extraction",
                )
            )
            continue

        bucket, existing_id = _classify_invoice(ctx, doc, entry.vendor, md5)
        counts[bucket] += 1

        if bucket == "NEW":
            cents = amount_to_cents(doc.amount)  # code computes; never the LLM
            invoice_id, is_new = store.insert_invoice(
                ctx.ledger,
                tenant=ctx.tenant_slug,
                vendor=entry.vendor,
                invoice_number=doc.invoice_number,
                amount_cents=cents,
                invoice_date=doc.invoice_date or "",
                due_date=doc.due_date,
                gl_account=entry.gl_account,
                cost_type=entry.cost_type,
                source_file=name,
                source_md5=md5,
                confidence=doc.confidence,
                shadow=ctx.shadow,
            )
            actions.append(f"recorded {entry.vendor} / {doc.invoice_number} ({bucket})")
            events.append(
                EventSpec(
                    key=f"new:{md5}",
                    event_type="ap.invoice.recorded",
                    payload={
                        "file": name,
                        "vendor": entry.vendor,
                        "invoice_number": doc.invoice_number,
                        "amount_cents": cents,
                        "invoice_id": invoice_id,
                        # Predict the filed name via the same helper `apply` uses,
                        # so the prediction matches the real filed file (amount
                        # formatted to cents, vendor slashes flattened).
                        "would_file_to": ctx.tenant.ap.filing_month_template.format(
                            month=_filed_month({"invoice_date": doc.invoice_date})
                        )
                        + "/"
                        + _filed_dest_name(
                            {
                                "vendor": entry.vendor,
                                "invoice_number": doc.invoice_number,
                                "amount_cents": cents,
                            }
                        ),
                    },
                )
            )
            # A successfully-extracted invoice auto-files (the `apply` job copies
            # the renamed original); no approval gate, since junk is already kept
            # out upstream (pre-filter + the unprocessed pile) and filing is not a
            # money move (2026-06-29). The payment decision still gates via status.
        elif bucket == "REVISED":
            store.append_note_once(
                ctx.ledger,
                invoice_id=existing_id,
                note=f"[Pending REVISED review] New amount {doc.amount} received; "
                f"row keeps its recorded amount until the owner swaps it.",
            )
            anomalies.append(
                Anomaly(
                    code="ap.revised_amount",
                    detail=f"{entry.vendor} / {doc.invoice_number}: new amount {doc.amount}",
                )
            )
            approvals.append(
                ApprovalSpec(
                    key=f"revised:{md5}",
                    action_type="ap.review_revised_invoice",
                    params={
                        "vendor": entry.vendor,
                        "invoice_number": doc.invoice_number,
                        "new_amount": str(doc.amount),
                        "file": name,
                    },
                    reason="amount differs from the recorded row; never auto-overwritten",
                )
            )
            events.append(
                EventSpec(
                    key=f"revised:{md5}",
                    event_type="ap.invoice.revised_received",
                    payload={
                        "file": name,
                        "vendor": entry.vendor,
                        "invoice_number": doc.invoice_number,
                    },
                )
            )
        else:  # DUPLICATE / ATTACH
            events.append(
                EventSpec(
                    key=f"{bucket.lower()}:{md5}",
                    event_type=f"ap.invoice.{bucket.lower()}",
                    payload={
                        "file": name,
                        "vendor": entry.vendor,
                        "invoice_number": doc.invoice_number,
                    },
                )
            )

    summary = ", ".join(f"{k} {v}" for k, v in counts.items() if v)
    return JobOutput(
        status="ok",
        summary=f"intake: {summary or 'nothing to process'}",
        actions=actions,
        events=events,
        approvals=approvals,
        anomalies=anomalies,
        rubric=score_intake(
            processed=extracted_count,
            validation_failures=validation_failures,
            # Intake queues no money actions: filing is not a money move (the
            # file-approval gate was dropped, 2026-06-29) and no intake approval
            # moves money. Both sides are 0, so human_gates reads 1.0 honestly
            # (nothing to gate, nothing bypassed). The payment phase will feed
            # real money actions here.
            money_actions=0,
            money_actions_queued=0,
            # Handoff completeness: every counted disposition left an event.
            # W-9 execution events are actions on approved cards, not
            # candidate dispositions, so they sit outside the count (02-F9);
            # so do provenance verdicts, which ride beside a disposition (#356).
            summary_present=sum(counts.values()) == len(events) - len(w9_events) - provenance_count,
        ),
    )


# ---------- verify-payment ---------------------------------------------------


def _verify_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "verify")
    for param in ("bank_csv", "billpay"):
        value = key.param(param)
        if value:
            # Same cloud-only sentinel as _intake_key; an unreadable input
            # fails loudly in the job body, this just keeps the key total.
            key.files([Path(value)], label=f"file:{param}", digest=_md5)
    rows = store.rows_for_verification(ctx.ledger, ctx.tenant_slug)
    key.value("rows", [(r["payee"], r["invoice_ref"], r["status"]) for r in rows])
    # A vendors.toml change alters payee canonicalization and so the verdict;
    # the registry is a run input (2026-07-20). So is the bank CSV format.
    key.vendors(_vendors(ctx))
    key.config("bank_csv")
    return key.digest()


def _verify_run(ctx: JobContext) -> JobOutput:
    from ...adapters.bank_csv import parse_bank_csv, to_cleared_bank_state

    state: dict = {
        "ap_ledger": store.rows_for_verification(ctx.ledger, ctx.tenant_slug),
        "cleared_bank": [],
        "billpay_queue": [],
    }
    warnings: list[Anomaly] = []
    if ctx.params.get("bank_csv"):
        lines = parse_bank_csv(ctx.params["bank_csv"], ctx.tenant.bank_csv)
        state["cleared_bank"] = to_cleared_bank_state(lines)
    if ctx.params.get("billpay"):
        raw = json.loads(Path(ctx.params["billpay"]).read_text(encoding="utf-8"))
        entries = [BillpayEntry.model_validate(e) for e in raw]
        state["billpay_queue"] = [e.model_dump() for e in entries]
    else:
        # The 2026-05-21 gap: without a queue snapshot, payable classification
        # is deferred, never guessed.
        warnings.append(
            Anomaly(
                code="ap.billpay_snapshot_missing",
                detail="no bill-pay queue snapshot supplied; payable verdicts are "
                "NOT safe for a payment run",
            )
        )

    # Canonicalize payee spellings through the tenant registry so a committed
    # payment recorded under a legacy/alias spelling is not missed (invariant 4).
    registry = _vendors(ctx)
    verdict = classify_payable(state, registry=registry)
    orphans = orphan_checks(state, registry)
    lagging = status_lag_anomalies(state, registry)

    anomalies = list(warnings)
    anomalies += [
        Anomaly(code="ap.orphan_check", detail=f"cleared {o.get('check_ref')!r} has no row")
        for o in orphans
    ]
    anomalies += [
        Anomaly(
            code="ap.status_lag",
            detail=f"{a['payee']} / {a['invoice_ref']} reads {a['status']} but has "
            f"{a['evidence']} evidence",
        )
        for a in lagging
    ]

    approvals = []
    snapshot_ok = bool(ctx.params.get("billpay"))
    for (payee, ref), kind in verdict.items():
        if kind == "payable" and snapshot_ok:
            row = next(
                r for r in state["ap_ledger"] if r["payee"] == payee and r["invoice_ref"] == ref
            )
            approvals.append(
                ApprovalSpec(
                    key=f"pay:{row['payee']}:{ref}",
                    action_type="ap.payment_recommendation",
                    params={
                        "payee": row["payee"],
                        "invoice_ref": ref,
                        "amount": str(row["amount"]),
                    },
                    reason="three-way verified payable; execution is never automatic",
                )
            )

    counts = {"payable": 0, "committed": 0, "paid": 0}
    for kind in verdict.values():
        counts[kind] += 1
    events = [
        EventSpec(
            key="verdict",
            event_type="ap.verification.completed",
            payload={
                # (payee, ref)-keyed dict flattened for JSON; self-describing.
                "verdict": [
                    {"payee": payee, "invoice_ref": ref, "verdict": kind}
                    for (payee, ref), kind in sorted(verdict.items())
                ],
                "counts": counts,
                "orphans": len(orphans),
                "status_lag": len(lagging),
                "billpay_snapshot": snapshot_ok,
            },
        )
    ]
    return JobOutput(
        status="ok",
        summary=(
            f"verify-payment: {counts['payable']} payable, {counts['committed']} committed, "
            f"{counts['paid']} paid, {len(orphans)} orphan check(s), {len(lagging)} status lag(s)"
        ),
        actions=[f"verified {len(verdict)} ledger row(s) three-way"],
        events=events,
        approvals=approvals,
        anomalies=anomalies,
    )


# ---------- queue-status -----------------------------------------------------


def _queue_status_key(ctx: JobContext) -> str:
    pending = ctx.ledger.list_approvals(ctx.tenant_slug, status="pending")
    counts = store.status_counts(ctx.ledger, ctx.tenant_slug)
    key = RunKey(ctx, "queue-status")
    key.value("pending", [p["id"] for p in pending])
    key.value("counts", counts)
    return key.digest()


def _queue_status_run(ctx: JobContext) -> JobOutput:
    pending = ctx.ledger.list_approvals(ctx.tenant_slug, status="pending")
    counts = store.status_counts(ctx.ledger, ctx.tenant_slug)
    by_action: dict[str, int] = {}
    for p in pending:
        by_action[p["action_type"]] = by_action.get(p["action_type"], 0) + 1
    lines = [f"{count}x {action}" for action, count in sorted(by_action.items())]
    return JobOutput(
        status="ok",
        summary=(
            f"queue-status: {len(pending)} pending approval(s)"
            + (f" ({'; '.join(lines)})" if lines else "")
            + f"; invoice statuses: {counts or 'none'}"
        ),
        actions=[],
        events=[
            EventSpec(
                key="snapshot",
                event_type="ap.queue.status",
                payload={"pending": len(pending), "by_action": by_action, "statuses": counts},
            )
        ],
    )


# ---------- apply (execute approved file actions) ---------------------------


def _filing_dir(ctx: JobContext) -> Path:
    configured = ctx.params.get("filing_dir") or ctx.tenant.ap.filing_dir
    if not configured:
        raise ValueError("no filing directory: set [ap].filing_dir or pass --param filing_dir=PATH")
    return Path(configured)


class FilingEscape(ValueError):
    """A filing destination resolved outside the filing tree."""


_UNSAFE_SEGMENT_RE = re.compile(r"[/\\\x00-\x1f\x7f]")


def _segment(text: object) -> str:
    """One path segment from untrusted text: separators and control
    characters flattened to '-', no leading dot (security review 2026-10-03)."""
    return _UNSAFE_SEGMENT_RE.sub("-", str(text)).strip().lstrip(".").strip()


def _filed_dest_name(params: dict) -> str:
    """The clean filed name, always one path segment. The vendor AND the
    invoice number are flattened: the number is model-extracted document text,
    so `1/../../x` must never climb out of the filing tree."""
    vendor = _segment(params.get("vendor", ""))
    number = _segment(params.get("invoice_number", ""))
    amount = (params.get("amount_cents") or 0) / 100
    return f"{vendor} - Inv {number} - ${amount:.2f}.pdf"


_MONTH_RE = re.compile(r"^\d{4}-\d{2}")


def _filed_month(params: dict) -> str:
    """The month subfolder for a filed invoice, from its invoice date.

    Mirrors the tenant's legacy YYYY-MM filing layout (owner decision, 2026-07-09).
    A missing or malformed date files to ``_undated`` rather than guessing.
    """
    m = _MONTH_RE.match(str(params.get("invoice_date") or ""))
    return m.group(0) if m else "_undated"


def _file_invoice_copy(
    params: dict,
    *,
    landing_dir: Path,
    filing_dir: Path,
    guard: WriteGuard,
    shadow: bool,
    month_template: str = "{month}",
) -> tuple[Path, bool]:
    """Copy one recorded invoice's landing original into the filing tree.

    A COPY, never a move: the landing original is the audit artifact and is
    never moved or deleted (tenant.toml [ap]). The destination always passes
    the write guard, so a filing_dir that points inside a protected production
    surface is refused even live. In shadow nothing is written; the computed
    destination is still returned for the dry-run report.

    The filing tree may be the tenant's shared legacy structure (owner
    decision 2026-07-10), so filing never clobbers what is already there: a
    same-named, same-content file is adopted as the filed artifact without a
    second copy; a different-content collision files under a suffixed name.
    """
    dest = _dest_for(params, filing_dir, month_template)
    if not dest.resolve().is_relative_to(filing_dir.resolve()):
        raise FilingEscape(f"filing destination {dest} is outside {filing_dir}")
    guard.check_write(dest)  # refuses a protected destination, even live
    if shadow:
        return dest, False
    src = landing_dir / str(params["file"])
    if not src.exists():
        raise FileNotFoundError(f"landing original missing for filing: {src}")
    # Honesty audit 2026-09-03 (02-F6): the shared placement contract. A hash
    # of None (cloud-only placeholder) on either side raises CannotVerify for
    # the caller to defer; it used to compare None == None and adopt a file
    # it never read. The copy is read back before it is called filed.
    placed = place_copy(src, dest, guard=guard, shadow=False, hash_fn=_md5)
    return placed.dest, True


def _dest_for(params: dict, filing_dir: Path, month_template: str) -> Path:
    rel = month_template.format(month=_filed_month(params))
    return filing_dir / rel / _filed_dest_name(params)


def _row_params(row: dict) -> dict:
    return {
        "vendor": row["vendor"],
        "invoice_number": row["invoice_number"],
        "amount_cents": row["amount_cents"],
        "invoice_date": row["invoice_date"],
        "file": row["source_file"],
    }


def _md5_or_missing(path: Path) -> str | None:
    """Key digest for a filing input: cloud-only -> None (the key's sentinel),
    absent -> 'missing', else the content hash."""
    if not path.exists():
        return "missing"
    return _md5(path)


def _recorded_invoices(ctx: JobContext) -> list[dict]:
    """Recorded invoices with a source file to file (and a fingerprint)."""
    rows = ctx.ledger.conn.execute(
        "SELECT vendor, invoice_number, amount_cents, invoice_date, source_file, source_md5 "
        "FROM ap_invoices WHERE tenant = ? AND source_md5 != '' AND source_file != '' "
        "ORDER BY id",
        (ctx.tenant_slug,),
    ).fetchall()
    return [dict(r) for r in rows]


def _already_filed_md5s(ctx: JobContext) -> set[str]:
    return {
        e.get("payload", {}).get("md5")
        for e in ctx.ledger.read_event_log()
        if e.get("event_type") == "ap.invoice.filed"
    } - {None}


def _apply_key(ctx: JobContext) -> str:
    # Changes when the set of recorded invoices (or the destination/mode) changes,
    # so a freshly-recorded invoice triggers a run while a repeat is a no-op.
    key = RunKey(ctx, "apply")
    key.param("landing_dir")
    key.config("ap")
    key.add("filing_dir", str(_filing_dir(ctx)))
    key.value("recorded", sorted(r["source_md5"] for r in _recorded_invoices(ctx)))
    # Honesty audit 2026-09-03 (02-F6): a filing deferred on a cloud-only
    # original or destination must re-fire when the placeholder materializes,
    # so both sides are key inputs while a recorded row is still unfiled.
    already = _already_filed_md5s(ctx)
    pending = [r for r in _recorded_invoices(ctx) if r["source_md5"] not in already]
    landing, filing = _landing_dir(ctx), _filing_dir(ctx)
    template = ctx.tenant.ap.filing_month_template
    key.files(
        [landing / str(r["source_file"]) for r in pending],
        label="originals",
        digest=_md5_or_missing,
    )
    key.files(
        [_dest_for(_row_params(r), filing, template) for r in pending],
        label="destinations",
        digest=_md5_or_missing,
    )
    return key.digest()


def _apply_run(ctx: JobContext) -> JobOutput:
    """File every recorded invoice not yet filed: copy the renamed landing
    original into the filing tree. No approval gate, since a successfully-
    extracted invoice is real (junk is kept out upstream) and filing is not a
    money move. Shadow is a dry-run (writes nothing); already-filed files are
    never copied twice.
    """
    landing = _landing_dir(ctx)
    filing = _filing_dir(ctx)
    already = _already_filed_md5s(ctx)

    events: list[EventSpec] = []
    actions: list[str] = []
    anomalies: list[Anomaly] = []
    deferred = 0
    for row in _recorded_invoices(ctx):
        md5 = row["source_md5"]
        if md5 in already:
            continue  # this exact file was already filed; never copy twice
        params = _row_params(row)
        label = f"{row['vendor']} / {row['invoice_number']}"
        try:
            dest, copied = _file_invoice_copy(
                params,
                landing_dir=landing,
                filing_dir=filing,
                guard=ctx.guard,
                shadow=ctx.shadow,
                month_template=ctx.tenant.ap.filing_month_template,
            )
        except CannotVerify as exc:
            # A placeholder on either side: nothing is claimed, the row stays
            # unfiled and the key re-fires when the content lands.
            deferred += 1
            anomalies.append(
                Anomaly(
                    code="ap.cloud_only_deferred",
                    detail=f"{label}: cannot verify the filing {exc.side} "
                    f"({Path(exc.filename).name} is a cloud-only placeholder); "
                    "filing deferred until it materializes",
                )
            )
            continue
        except CopyMismatch as exc:
            deferred += 1
            anomalies.append(
                Anomaly(
                    code="ap.filing_unverified",
                    detail=f"{label}: the copy to {exc.dest.name} read back different "
                    "content; removed, retried next run",
                )
            )
            continue
        except FilingEscape as exc:
            deferred += 1
            anomalies.append(Anomaly(code="ap.filing_refused", detail=f"{label}: {exc}"))
            continue
        verb = "filed" if copied else "would file"
        actions.append(f"{verb} {row['vendor']} / {row['invoice_number']} -> {dest.name}")
        if copied:
            events.append(
                EventSpec(
                    key=f"filed:{md5}",
                    event_type="ap.invoice.filed",
                    payload={
                        "file": row["source_file"],
                        "vendor": row["vendor"],
                        "invoice_number": row["invoice_number"],
                        "md5": md5,
                        "filed_to": str(dest),
                    },
                )
            )
    verb = "would file" if ctx.shadow else "filed"
    summary = f"apply: {verb} {len(actions)}; {len(already)} already filed"
    if deferred:
        summary += f"; {deferred} deferred (cannot verify)"
    return JobOutput(
        status="ok",
        summary=summary,
        actions=actions,
        events=events,
        anomalies=anomalies,
    )


# ---------- workbook (human-readable delivery view) -------------------------


def _workbook_out_path(ctx: JobContext) -> Path:
    configured = ctx.params.get("workbook_path") or ctx.tenant.ap.workbook_path
    if not configured:
        raise ValueError(
            "no workbook path: set [ap].workbook_path or pass --param workbook_path=PATH"
        )
    return Path(configured)


def _workbook_key(ctx: JobContext) -> str:
    from .unprocessed import unresolved_unprocessed
    from .workbook import VIEW_VERSION

    rows = ctx.ledger.conn.execute(
        "SELECT id, status, amount_cents, updated_at FROM ap_invoices WHERE tenant = ? ORDER BY id",
        (ctx.tenant_slug,),
    ).fetchall()
    unprocessed = sorted(u["md5"] for u in unresolved_unprocessed(ctx.ledger, ctx.tenant_slug))
    key = RunKey(ctx, "workbook", version=VIEW_VERSION)  # a renderer change regenerates
    key.rows("rows", rows)
    key.config("ap", "identity")  # columns, output path, the metadata legal name
    key.add("out", str(_workbook_out_path(ctx)))
    key.value("unprocessed", unprocessed)  # dismiss/identify changes this -> regenerate
    return key.digest()


def _workbook_run(ctx: JobContext) -> JobOutput:
    from .view_refresh import clear
    from .workbook import run_workbook

    columns = ctx.tenant.ap.workbook_columns
    if not columns:
        raise ValueError("no workbook columns configured ([ap].workbook_columns)")
    if ctx.shadow:  # shadow writes nothing on the owner's disk (#326)
        return JobOutput(status="ok", summary=f"workbook: would write {_workbook_out_path(ctx)}")
    written = run_workbook(
        tenant_slug=ctx.tenant_slug,
        ledger=ctx.ledger,
        columns=columns,
        legal_name=ctx.tenant.identity.legal_name,
        guard=ctx.guard,
        out_path=_workbook_out_path(ctx),
    )
    clear(ctx.ledger.root)  # the view now matches the ledger
    return JobOutput(
        status="ok",
        summary=f"workbook: wrote {written}",
        actions=[f"rendered workbook view -> {written}"],
    )


# ---------- dismiss / identify (resolve the unprocessed pile) ---------------


def _dismiss_key(ctx: JobContext) -> str:
    from .unprocessed import unresolved_unprocessed

    key = RunKey(ctx, "dismiss")
    if key.param("all"):
        pile = unresolved_unprocessed(ctx.ledger, ctx.tenant_slug)
        key.value("md5s", sorted(i["md5"] for i in pile))
    else:
        key.param("file")
    return key.digest()


def _dismissed_event(file: str, md5: str) -> EventSpec:
    return EventSpec(
        key=f"dismiss:{md5}",
        event_type="ap.intake.dismissed",
        payload={"file": file, "md5": md5},
    )


def _dismiss_run(ctx: JobContext) -> JobOutput:
    from .unprocessed import md5_for_file, unresolved_unprocessed

    if ctx.params.get("all"):
        items = unresolved_unprocessed(ctx.ledger, ctx.tenant_slug)
        if not items:
            return JobOutput(status="ok", summary="dismiss --all: nothing unprocessed")
        return JobOutput(
            status="ok",
            summary=f"dismissed {len(items)} unprocessed files (not invoices)",
            actions=[f"dismissed {i['file']}" for i in items],
            events=[_dismissed_event(i["file"], i["md5"]) for i in items],
        )

    file = ctx.params.get("file")
    if not file:
        raise ValueError("dismiss requires a file or --all")
    md5 = md5_for_file(ctx.ledger, file)
    if not md5:
        return JobOutput(
            status="error",
            summary=f"no unprocessed file named {file!r}",
            anomalies=[Anomaly(code="ap.dismiss_not_found", detail=file)],
        )
    return JobOutput(
        status="ok",
        summary=f"dismissed {file} (not an invoice; will not resurface)",
        actions=[f"dismissed {file}"],
        events=[_dismissed_event(file, md5)],
    )


def _identify_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "identify")
    for name in ("file", "vendor", "invoice_number", "amount"):
        key.param(name)
    return key.digest()


def _identify_run(ctx: JobContext) -> JobOutput:
    from .unprocessed import md5_for_file

    p = ctx.params
    file, vendor, number, amount = (
        p.get("file"),
        p.get("vendor"),
        p.get("invoice_number"),
        p.get("amount"),
    )
    if not all([file, vendor, number, amount]):
        raise ValueError("identify requires --param file=, vendor=, invoice_number=, amount=")
    md5 = md5_for_file(ctx.ledger, file)
    if not md5:
        return JobOutput(
            status="error",
            summary=f"no unprocessed file named {file!r}",
            anomalies=[Anomaly(code="ap.identify_not_found", detail=file)],
        )
    try:
        cents = amount_to_cents(Decimal(str(amount)))
    except Exception as exc:
        raise ValueError(f"identify: bad amount {amount!r}: {exc}") from exc
    invoice_id, _ = store.insert_invoice(
        ctx.ledger,
        tenant=ctx.tenant_slug,
        vendor=vendor,
        invoice_number=number,
        amount_cents=cents,
        source_file=file,
        source_md5=md5,
        shadow=ctx.shadow,
    )
    return JobOutput(
        status="ok",
        summary=f"identified {file} as {vendor} / {number} (${cents / 100:,.2f})",
        actions=[f"identified {file} -> {vendor} / {number}"],
        events=[
            EventSpec(
                key=f"identify:{md5}",
                event_type="ap.invoice.recorded",
                payload={
                    "file": file,
                    "vendor": vendor,
                    "invoice_number": number,
                    "amount_cents": cents,
                    "md5": md5,
                    "invoice_id": invoice_id,
                    "source": "manual identify",
                },
            )
        ],
    )


# ---------- janitor (landing-folder organization) ---------------------------


# Job-time durable record of an archive move (honesty audit #170, 03-F9):
# written before the file moves, healed into ap.landing.archived next run
# if the run died in between.
JANITOR_RECORD = "ap.landing.archived.recorded"


def _janitor_days(ctx: JobContext) -> int:
    return int(ctx.params.get("days") or 30)


# Why a file became archive material, in the order ``_janitor_eligible``
# applies its rules. The reason travels with the path so the summary names the
# rule that actually fired instead of the one input it happened to have: a run
# whose whole set was junk-by-rule or ledger-settled used to report it as
# "archived N aged file(s) (older than Dd)", which is a measurement nobody
# made (docs/lessons.md, "A finding explains only what it measured").
JANITOR_JUNK = "junk"
JANITOR_AGED = "aged"
JANITOR_SETTLED = "settled"


def _janitor_in_flight_names(ctx: JobContext) -> set[str]:
    """Landing names the janitor must never archive: files apply/identify
    still resolve by name. An unfiled recorded invoice's original (apply
    copies it later) and any file named in a pending approval (identify /
    OCR review acts on it) are work in progress, whatever their age."""
    filed = _already_filed_md5s(ctx)
    names = {r["source_file"] for r in _recorded_invoices(ctx) if r["source_md5"] not in filed}
    for item in ctx.ledger.pending_approvals(ctx.tenant_slug):
        # Only AP approvals pin a landing file: identify/apply resolve it by
        # name later. Another agent's card (e.g. timesheets.payroll_hours)
        # references an already-recorded, already-filed document, so the
        # landing original is archive material once that agent settles it.
        if not str(item.get("action_type", "")).startswith("ap."):
            continue
        file = (item.get("params") or {}).get("file")
        if file:
            names.add(str(file))
    return names


def _janitor_settled_md5s(ctx: JobContext) -> set[str]:
    """Content hashes the ledger has fully dispositioned: filed, dismissed,
    routed to another flow, or recognized as a duplicate/attach. The raw
    landing original of a settled document is pure archive material."""
    settled: set[str] = set()
    for e in ctx.ledger.read_event_log():
        et = e.get("event_type", "")
        payload = e.get("payload") or {}
        if et in ("ap.invoice.filed", "ap.intake.dismissed") and payload.get("md5"):
            settled.add(payload["md5"])
        elif et in ("timesheets.recorded", "timesheets.companion_filed"):
            # another agent claimed and recorded it; its raw copy is archive
            # material like any settled document
            md5 = str(e.get("idempotency_key", "")).rsplit(":", 1)[-1]
            if len(md5) == 32:
                settled.add(md5)
        elif et.startswith("ap.intake.routed.") or et in (
            "ap.invoice.duplicate",
            "ap.invoice.attach",
        ):
            # md5 rides in the event key suffix: "...:evt:route:<md5>"
            key = e.get("idempotency_key", "")
            md5 = key.rsplit(":", 1)[-1]
            if len(md5) == 32:
                settled.add(md5)
    return settled


def _janitor_eligible(ctx: JobContext) -> list[tuple[Path, str]]:
    """Top-level landing files whose disposition is settled, plus anything
    older than the age cutoff, each paired with the rule that caught it.
    Never descends into subfolders (``_archive``, ``_skipped``, ... are already
    organized).

    Disposition first (owner feedback 2026-07-09: ~80% of arrivals are junk
    the engine has already judged, and age alone left them visible for days):
    deterministic junk (inline-graphic name, non-candidate suffix) and
    ledger-settled documents (filed / dismissed / routed / duplicate) archive
    the day they arrive. The age cutoff only catches the leftovers, e.g. a
    flagged extraction that never got resolved. In-flight names always stay.

    A file can satisfy more than one rule; the reason returned is the first
    one that made it eligible, which is the rule the run acted on and so the
    only one its summary may cite.
    """
    import time as _time

    landing = _landing_dir(ctx)
    if not landing.is_dir():
        raise MissingFolderError(f"landing directory {landing} does not exist")
    cutoff = _time.time() - _janitor_days(ctx) * 86400
    in_flight = _janitor_in_flight_names(ctx)
    settled = _janitor_settled_md5s(ctx)
    out: list[tuple[Path, str]] = []
    for path in sorted(landing.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name.endswith(".extract.json"):
            continue  # fixture sidecars are metadata, not archive material
        if path.name in in_flight:
            continue
        if _is_timesheet_name(path.name):
            # the timesheets agent's intake; junk-by-suffix does not apply.
            # Archived only once recorded (settled) or via the age backstop.
            pass
        elif path.suffix.lower() not in AP_CANDIDATE_SUFFIXES or _looks_like_inline_graphic(
            path.name
        ):
            out.append((path, JANITOR_JUNK))  # junk by deterministic rule, any age
            continue
        if path.stat().st_mtime < cutoff:
            out.append((path, JANITOR_AGED))  # aged out of the intake window
            continue
        digest = _md5(path)  # cloud-only placeholder: leave for the age rule
        if digest and digest in settled:
            out.append((path, JANITOR_SETTLED))
    return out


def _janitor_key(ctx: JobContext) -> str:
    """Same eligible set = same run = no-op; each archive pass changes the set.

    The label and the value shape are deliberately untouched (``aged``, and
    ``name:mtime`` over the same paths in the same order): the reason each file
    became eligible is a fact about the summary, not a new run input, so no
    janitor key moves and nothing re-fires on this change.
    """
    key = RunKey(ctx, "janitor")
    key.add("days", str(_janitor_days(ctx)))
    key.config("ap")
    key.value("aged", [f"{p.name}:{int(p.stat().st_mtime)}" for p, _ in _janitor_eligible(ctx)])
    return key.digest()


def _archive_move(
    src: Path, month_dir: Path, *, guard: WriteGuard, shadow: bool, record=None
) -> Path:
    """Move one aged file into its archive month folder.

    A move WITHIN the landing tree: the original is organized, never deleted
    and never taken out of the tree (owner decision 2026-07-09, amending the
    original never-moved rule). The destination
    passes the write guard, so archiving requires an explicit ``_archive``
    carve-out in ``allowed_paths``. A name collision gets a numeric suffix;
    an existing archived file is never overwritten.
    """
    dest = month_dir / src.name
    n = 2
    while dest.exists():
        dest = month_dir / f"{src.stem} ({n}){src.suffix}"
        n += 1
    guard.check_write(dest)
    if shadow:
        return dest
    month_dir.mkdir(parents=True, exist_ok=True)
    if record is not None:
        # Record-then-move (03-F9): the destination is on the record before
        # the file leaves the top level, so a death after the move is a
        # record the next run heals into the event, never a silent move.
        record(dest)
    shutil.move(str(src), str(dest))
    return dest


def _janitor_run(ctx: JobContext) -> JobOutput:
    """Organize settled and aged top-level landing files into ``_archive/YYYY-MM``.

    The month comes from each file's own mtime (when it arrived), so the
    archive reads like a mail log. Intake ignores subfolders, so archived
    files stop being candidates; ledger md5 dedup still recognizes a re-send.
    Content is never read (a cloud-only placeholder moves fine unhydrated).

    The summary counts the files this pass actually moved, one clause per rule
    that fired, so it can never cite the age cutoff on a file that landed
    seconds ago; a healed archive record is reported as itself, not as a file
    archived now.
    """
    import time as _time

    landing = _landing_dir(ctx)
    archive = landing / "_archive"
    events: list[EventSpec] = []
    actions: list[str] = []
    anomalies: list[Anomaly] = []

    # 03-F9, the healed half: an archive recorded at job time whose event
    # never landed (the run died after the move) gets its event now, from
    # the record, so the mail log stays complete.
    archived_keys = {
        f"{e.get('payload', {}).get('file', '')}:{e.get('payload', {}).get('mtime', '')}"
        for e in ctx.ledger.read_event_log()
        if e.get("event_type") == "ap.landing.archived"
    }
    healed = 0
    for rec in ctx.records(JANITOR_RECORD):
        payload = rec["payload"]
        rec_key = f"{payload.get('file', '')}:{payload.get('mtime', '')}"
        if rec_key in archived_keys:
            continue
        archived_keys.add(rec_key)
        events.append(
            EventSpec(
                key=f"archive:{payload.get('file', '')}:{payload.get('mtime', '')}",
                event_type="ap.landing.archived",
                payload=payload,
            )
        )
        healed += 1
        actions.append(
            f"recorded the archive of {payload.get('file', '')} from its job record "
            "(a run died between the move and the record)"
        )

    moved_by_reason: dict[str, int] = {}
    for path, reason in _janitor_eligible(ctx):
        mtime = int(path.stat().st_mtime)
        month = _time.strftime("%Y-%m", _time.localtime(mtime))

        def _record(dest: Path, *, _name=path.name, _mtime=mtime) -> None:
            ctx.record_now(
                f"archive:{_name}:{_mtime}",
                JANITOR_RECORD,
                {"file": _name, "archived_to": str(dest), "mtime": _mtime},
            )

        try:
            dest = _archive_move(
                path, archive / month, guard=ctx.guard, shadow=ctx.shadow, record=_record
            )
        except ProtectedSurfaceError:
            # A guard refusal (no _archive carve-out) never clears on a retry
            # and is a job error, not a per-file move failure (02-F8).
            raise
        except OSError as exc:
            anomalies.append(Anomaly(code="ap.janitor_move_failed", detail=f"{path.name}: {exc}"))
            continue
        verb = "archived" if not ctx.shadow else "would archive"
        moved_by_reason[reason] = moved_by_reason.get(reason, 0) + 1
        actions.append(f"{verb} {path.name} -> _archive/{month}/")
        if not ctx.shadow:
            events.append(
                EventSpec(
                    key=f"archive:{path.name}:{mtime}",
                    event_type="ap.landing.archived",
                    payload={"file": path.name, "archived_to": str(dest), "mtime": mtime},
                )
            )
    verb = "would archive" if ctx.shadow else "archived"
    # One clause per rule that fired, and nothing about the rules that did
    # not: a pass of junk and settled files says so, a pass that archived
    # nothing claims no criterion at all, and the age cutoff is named only
    # when a file really crossed it.
    clauses = [
        f"{moved_by_reason[reason]} {label}"
        for reason, label in (
            (JANITOR_JUNK, "junk by rule"),
            (JANITOR_AGED, f"aged past {_janitor_days(ctx)}d"),
            (JANITOR_SETTLED, "settled in the ledger"),
        )
        if moved_by_reason.get(reason)
    ]
    summary = f"janitor: {verb} {sum(moved_by_reason.values())} file(s)"
    if clauses:
        summary += ": " + ", ".join(clauses)
    if healed:
        summary += f"; healed {healed} archive record(s) from a run that died mid-move"
    return JobOutput(
        status="ok",
        summary=summary,
        actions=actions,
        events=events,
        anomalies=anomalies,
    )


# ---------- reconcile: cleared QBO payments settle committed rows ----------

# Evidence fetched during key computation, handed to the run for the same ctx
# (the runner calls key() then run() on one context; fetching twice would race
# the API against itself and could hash different inputs than it acts on).
_RECONCILE_EVIDENCE: dict[int, list] = {}


def _reconcile_evidence(ctx: JobContext) -> list:
    """Normalized cleared-payment evidence: a JSON file (tests, replay) or the
    live QBO query. The file seam keeps every eval off the network."""
    from ...adapters.qbo import QboClient, QboEvidence

    override = ctx.params.get("evidence_file")
    if override:
        return [QboEvidence(**e) for e in json.loads(Path(override).read_text())]
    token_file = ctx.params.get("qbo_token_file") or ctx.tenant.qbo.token_file
    if not token_file:
        raise ValueError(
            "no QBO token file: set [qbo].token_file in tenant.toml or pass "
            "--param qbo_token_file=PATH (or replay with --param evidence_file=PATH)"
        )
    since = ctx.params.get("since")
    if not since:
        from datetime import date, timedelta

        from ...engine.clock import local_today

        # The tenant's wall-clock today, not the host's (#136): correct on
        # any machine, not only one whose system tz matches the tenant.
        today = date.fromisoformat(local_today(ctx.tenant.identity.timezone))
        since = (today - timedelta(days=ctx.tenant.qbo.since_days)).isoformat()
    return QboClient(token_file).fetch_evidence(since=since)


def _reconcile_rows(ctx: JobContext) -> list[dict]:
    """Every row in the tenant's book, INCLUDING shadow-born ones: pre-cutover
    shadow intake created rows that are the live book (2026-07-16 miss).
    ``scheduled_at`` is the latest REAL transition into a committed status —
    row-birth history ('(new row)') is an import/test artifact, not evidence
    of when money was committed — and feeds the chronology guard."""
    rows = ctx.ledger.conn.execute(
        """
        SELECT i.*,
               (SELECT MAX(h.created_at) FROM ap_status_history h
                 WHERE h.invoice_id = i.id
                   AND h.status_to IN ('Scheduled', 'Scheduled in bill pay')
                   AND h.status_from != '(new row)') AS scheduled_at
          FROM ap_invoices i WHERE i.tenant = ? ORDER BY i.id
        """,
        (ctx.tenant_slug,),
    ).fetchall()
    return [dict(r) for r in rows]


def _reconcile_key(ctx: JobContext) -> str:
    """Same evidence + same row states = same run. Shadow and live hash apart
    on purpose: shadow is a dry look at inputs the live run must then act on."""
    evidence = _reconcile_evidence(ctx)
    _RECONCILE_EVIDENCE[id(ctx)] = evidence
    key = RunKey(ctx, "reconcile")
    key.value("evidence", sorted(f"{e.qbo_id}:{e.amount_cents}" for e in evidence))
    key.value("rows", [f"{r['id']}:{r['status']}" for r in _reconcile_rows(ctx)])
    # Found by the #153 audit: the ignore list (config + --param) and the
    # vendor registry steer which evidence settles which row, and neither
    # was in the key — adding an ignored payee replayed the prior run.
    key.param("ignore_payees")
    key.param("trace_floor_cents")
    # #285: the bill-pay channel list steers which statement lines settle
    # which committed rows, so it is an input like the ignore list. The
    # [qbo] section itself is declared below.
    key.param("bill_pay_channels")
    key.vendors(_vendors(ctx))
    key.config("qbo")
    # #281: the engine's own expense-report Purchases steer which clearings
    # are already recorded, and their totals decide whether an amount drift
    # is worth a note. A report recorded since the last run is new input.
    key.value(
        "expense_purchases",
        sorted(f"{pid}:{r['total_cents']}" for pid, r in _expense_report_purchases(ctx).items()),
    )
    # 7.3: the statement file is evidence, declared by content: an export
    # with a new line re-fires, the same bytes re-touched replay, a path that
    # is not there hashes as missing. The [bank_csv] section (format and the
    # folder the daily run resolves) decides how the file reads.
    csv = str(key.param("bank_csv") or "")
    if csv:
        key.file(Path(csv).expanduser(), label="bank_csv")
    key.config("bank_csv")
    # 7.4: the same rule for a whole folder — every file the selector picks,
    # by name and content. A new statement is new evidence; an unchanged
    # folder replays. A folder that cannot be LISTED is the one case that
    # must never replay: the key takes a stamp so the run executes again and
    # says so again, every morning, until somebody fixes the permission.
    stmt_dir = str(key.param("statement_dir") or "")
    if stmt_dir:
        from ...adapters.bank_statement_pdf import statement_files

        try:
            key.files(
                statement_files(Path(stmt_dir).expanduser(), ctx.tenant.bank_csv),
                label="statement_dir",
            )
        except OSError as exc:
            from datetime import UTC, datetime

            key.value(
                "statement_dir",
                f"unreadable: {exc.strerror or exc} @ {datetime.now(UTC).isoformat()}",
            )
    # 7.1: an owner-resolved review card is a run input. Approval alone
    # changes no row state, so without this the approval never re-fires the
    # job and the chosen row stays Scheduled until something else moves.
    executed = _review_executed_card_ids(ctx)
    key.value(
        "review_exec",
        sorted(
            f"{c['id']}:{c['params'].get('row', '')}"
            for c in _approved_review_cards(ctx)
            if c["id"] not in executed
        ),
    )
    # 7.4: same shape for the hand-check lane. An approved card creates a row
    # and nothing else in this key changes when it does, so without this the
    # owner's approval would sit unexecuted until unrelated evidence moved.
    key.param("direct_payment_cards")
    key.param("direct_payment_floor_cents")
    dp_done = _direct_payment_executed_ids(ctx)
    key.value(
        "direct_payment_exec",
        sorted(
            f"{c['id']}:{c['params'].get('qbo_id', '')}"
            for c in _approved_direct_payment_cards(ctx)
            if str(c["params"].get("qbo_id", "")) not in dp_done
        ),
    )
    return key.digest()


def _reconcile_explained(ctx: JobContext) -> tuple[set[str], set[str]]:
    """QBO transaction ids this ledger has already been through, in two
    parts: ``(answered, flagged_only)``.

    ANSWERED is a real answer: the clearing settled a row, or a review card
    naming it was decided. FLAGGED-ONLY means the only thing that ever
    happened to the id is an ``ap.reconcile.unknown`` event, which is the
    UNANSWERED state written down once so the owner is not asked twice
    (#287, and #293 for this side of it).

    Both skip the decision loop, exactly as before: evidence stays in the
    query window for weeks, and without this memory every past clearing
    would re-flag as unknown money on every later run. Only the answered
    part also skips the hand-check lane. An id that is both is answered: the
    answer came after the question.
    """
    answered: set[str] = set()
    flagged: set[str] = set()
    for e in ctx.ledger.read_event_log():
        event_type = e.get("event_type")
        if event_type not in ("ap.reconcile.paid", "ap.reconcile.unknown"):
            continue
        qbo_id = (e.get("payload") or {}).get("qbo_id")
        if not qbo_id:
            continue
        if event_type == "ap.reconcile.paid":
            answered.add(str(qbo_id))
        else:
            flagged.add(str(qbo_id))
    rows = ctx.ledger.conn.execute(
        "SELECT params_json FROM approval_queue WHERE action_type = ? AND status != 'pending'",
        (REVIEW_CARD,),
    ).fetchall()
    for row in rows:
        params = json.loads(row["params_json"])
        qbo_id = params.get("qbo_id")
        if qbo_id:
            answered.add(str(qbo_id))
        # Group review cards (issue #115) carry every payment they answer.
        for one in str(params.get("qbo_ids", "")).split(","):
            if one.strip():
                answered.add(one.strip())
    return answered, flagged - answered


EXPENSE_DRIFT_EVENT = "ap.reconcile.expense_amount_drift"


def _expense_report_purchases(ctx: JobContext) -> dict[str, dict]:
    """Every accounting-system Purchase the engine wrote for one of this
    tenant's expense reports, by its raw id (issue #281).

    An expense report commits money with no ``ap_invoices`` row, so its
    clearing has nothing in the AP book to match and used to land in unknown
    money every single time. ``qbo_purchase_id`` is the exact id the engine
    itself recorded at ``expenses match``: identity, not a heuristic.
    """
    rows = ctx.ledger.conn.execute(
        "SELECT id, person, month, total_cents, status, qbo_purchase_id "
        "FROM expense_report WHERE tenant = ? AND qbo_purchase_id IS NOT NULL "
        "AND qbo_purchase_id != ''",
        (ctx.tenant_slug,),
    ).fetchall()
    return {str(r["qbo_purchase_id"]): dict(r) for r in rows}


def _expense_drift_recorded(ctx: JobContext) -> set[str]:
    """The amount disagreements this ledger has already noted, as
    ``<qbo id>:<report cents>:<cleared cents>``. The event key is namespaced
    under the run key, so only the log itself can keep an informational note
    from repeating every morning."""
    recorded: set[str] = set()
    for e in ctx.ledger.read_event_log():
        if e.get("event_type") != EXPENSE_DRIFT_EVENT:
            continue
        payload = e.get("payload") or {}
        recorded.add(
            f"{payload.get('qbo_id', '')}:{payload.get('report_total_cents', '')}"
            f":{payload.get('cleared_amount_cents', '')}"
        )
    return recorded


# ---- the statement tier (phase 7 rows 7.3 and 7.4, issues #212 + #213) ------
#
# Once the engine records a BillPayment itself (7.2), the owner's Match click
# in the bank feed is invisible to the API and rule 3 keeps the engine's own
# record out of QBO evidence, so nothing above can settle those rows. The
# bank's statement can: a check line matching the payment on reference,
# amount, and date window flips every row the payment covers, with the
# statement's cleared date.
#
# 7.4 (2026-09-16) made the whole folder the evidence rather than one export.
# The monthly statement PDF lands on the 1st with no owner effort; a CSV
# export happens only when somebody remembers (once in nine months on the
# live tenant, while nine unread statements holding 107 check numbers sat
# beside it). ``--param statement_dir`` reads every statement file each run,
# PDFs and CSVs alike; ``--param bank_csv`` still names one file; neither is
# one log line. Line identity (date + cents + reference) makes re-reading
# the same months idempotent, which is what lets the tier be greedy.

STATEMENT_UNKNOWN_SOURCE = "statement"
STATEMENT_LINES_EVENT = "ap.reconcile.statement_lines"
STATEMENT_UNPARSED = "ap.reconcile.statement_unparsed"
STATEMENT_UNREADABLE = "ap.reconcile.statement_unreadable"
REF_BACKFILL_EVENT = "ap.reconcile.ref_backfilled"


@dataclass
class _StatementRead:
    """What the statement folder gave this run: the check lines the tier can
    answer, one log line per file, the per-file section counts (recorded, so
    the parsed withdrawals and deposits are on the record without changing
    any behaviour), and the anomalies of files that could not be read."""

    evidence: list = dataclasses_field(default_factory=list)
    notes: list[str] = dataclasses_field(default_factory=list)
    files: list[dict] = dataclasses_field(default_factory=list)
    anomalies: list = dataclasses_field(default_factory=list)


def _statement_paths(ctx: JobContext) -> tuple[list[Path], list[str], list]:
    """(files to read, log lines, anomalies). ``statement_dir`` wins over the
    single-file ``bank_csv``; neither is a skip, never an anomaly (most
    mornings a tenant has no statement at all)."""
    from ...adapters.bank_statement_pdf import statement_files

    raw_dir = str(ctx.params.get("statement_dir") or "")
    raw_file = str(ctx.params.get("bank_csv") or "")
    if raw_dir:
        directory = Path(raw_dir).expanduser()
        try:
            picked = statement_files(directory, ctx.tenant.bank_csv)
        except OSError as exc:
            # 2026-09-13: macOS gates readdir per program identity and the
            # denial is silent. "I could not look" is never "nothing to
            # look at" — every engine-written payment settles from here.
            return (
                [],
                [f"statement tier: COULD NOT LIST {directory}"],
                [
                    Anomaly(
                        code=STATEMENT_UNREADABLE,
                        detail=(
                            f"could not list the statement folder {directory}: {exc}. This is "
                            "not 'no statement this morning' — the folder was not read. Check "
                            "that the path exists and that the host lets the interpreter read "
                            "it (on macOS, its Full Disk Access grant)."
                        ),
                    )
                ],
            )
        if not picked:
            return [], [f"statement tier: no statement file in {directory}"], []
        return picked, [], []
    if not raw_file:
        return [], ["statement tier skipped: no statement_dir or bank_csv param"], []
    path = Path(raw_file).expanduser()
    if not path.is_file():
        return [], [f"statement tier skipped: no statement file at {path.name}"], []
    return [path], [], []


def _statement_lines(ctx: JobContext) -> _StatementRead:
    """Read every statement file this run was pointed at.

    A file that is not a statement at all (the stray card statement, the
    drawing somebody filed in the folder) is named in the log and skipped; a
    file that IS a statement but does not tie to its own printed counts and
    totals is an anomaly, never a silent skip, because the lines a broken
    parse dropped look exactly like money that never moved.
    """
    from ...adapters.bank_statement_pdf import (
        BankStatementError,
        StatementNotRecognized,
        parse_statement_file,
    )
    from .reconcile import statement_evidence

    paths, notes, anomalies = _statement_paths(ctx)
    read = _StatementRead(notes=notes, anomalies=anomalies)
    for path in paths:
        try:
            sections = parse_statement_file(path, ctx.tenant.bank_csv)
        except StatementNotRecognized as exc:
            read.notes.append(f"statement tier: skipped {path.name} (not a bank statement)")
            del exc
            continue
        except BankStatementError as exc:
            read.anomalies.append(Anomaly(code=STATEMENT_UNPARSED, detail=str(exc)))
            continue
        evidence = statement_evidence(sections.lines)
        read.evidence.extend(evidence)
        counts = sections.counts()
        read.files.append(
            {"file": path.name, "period_end": sections.period_end.isoformat(), **counts}
        )
        read.notes.append(
            f"statement file {path.name}: {counts['checks']} check, "
            f"{counts['withdrawals']} withdrawal, {counts['deposits']} deposit line(s); "
            f"{len(evidence)} clearing candidate(s)"
        )
    return read


def _statement_explained(ctx: JobContext) -> tuple[set[str], set[str]]:
    """Statement line identities this ledger has already been through, in two
    parts: ``(answered, flagged_only)``.

    ANSWERED is a real answer: the line settled a row, its reference landed
    on a settled row, or a review card naming it was decided. FLAGGED-ONLY
    means the only thing that ever happened to the line is an
    ``ap.reconcile.unknown`` event, which is the UNANSWERED state written
    down once so the owner is not asked twice, not an answer (#287).

    Both skip the decision loop, exactly as before: a later, longer export
    carries every earlier line again, and the once-only unknown must stay
    once-only. Only the answered part also skips the hand-check lane. A line
    that is both is answered: the answer came after the question.
    """
    answering = (
        "ap.reconcile.paid",
        REF_BACKFILL_EVENT,
        REVIEW_PAID_EVENT,
    )
    answered: set[str] = set()
    flagged: set[str] = set()
    for e in ctx.ledger.read_event_log():
        sid = (e.get("payload") or {}).get("statement_id")
        if not sid:
            continue
        event_type = e.get("event_type")
        if event_type in answering:
            answered.add(str(sid))
        elif event_type == "ap.reconcile.unknown":
            flagged.add(str(sid))
    rows = ctx.ledger.conn.execute(
        "SELECT params_json FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? AND status != 'pending'",
        (ctx.tenant_slug, REVIEW_CARD),
    ).fetchall()
    for row in rows:
        sid = json.loads(row["params_json"]).get("statement_id")
        if sid:
            answered.add(str(sid))
    return answered, flagged - answered


def _flagged_unknown_checks(ctx: JobContext) -> dict[tuple[str, int], set[str]]:
    """(normalized check, cents) -> the sources that already flagged that
    check as unknown money. One physical check clears in the feed AND on the
    statement; the owner should mute it once, whichever source saw it first."""
    from .reconcile import norm_check_ref

    flagged: dict[tuple[str, int], set[str]] = {}
    for e in ctx.ledger.read_event_log():
        if e.get("event_type") != "ap.reconcile.unknown":
            continue
        payload = e.get("payload") or {}
        check = norm_check_ref(str(payload.get("check_ref") or ""))
        if not check:
            continue
        key = (check, int(payload.get("amount_cents") or 0))
        flagged.setdefault(key, set()).add(str(payload.get("source") or "qbo"))
    return flagged


def _carded_checks(ctx: JobContext) -> set[tuple[str, int]]:
    """(normalized check, cents) pairs this engine has already put in front of
    the owner, whatever the card's status and whichever source parked it. One
    physical check clears in the accounting feed, on the bank statement, AND
    in the sweep's parked rows; the owner answers it once (7.4, the second
    and third sources)."""
    from .reconcile import norm_check_ref

    rows = ctx.ledger.conn.execute(
        "SELECT params_json FROM approval_queue WHERE action_type IN (?, ?)",
        (DIRECT_PAYMENT_CARD, SWEEP_CARD),
    ).fetchall()
    carded: set[tuple[str, int]] = set()
    for row in rows:
        params = json.loads(row["params_json"])
        check = norm_check_ref(str(params.get("check_ref") or ""))
        if check:
            carded.add((check, int(params.get("amount_cents") or 0)))
    return carded


def _lane_cards(ctx: JobContext) -> list[dict]:
    """Every hand-check card this ledger holds, whatever its status, reduced
    to what the cross-source identity reads (#305).

    Status is deliberately not filtered: a card the owner already answered
    still answers its physical check, and a pending one is still in front of
    him. Either way the other source must not ask the same question again.
    """
    from . import direct_payment as dp

    rows = ctx.ledger.conn.execute(
        "SELECT id, status, params_json FROM approval_queue WHERE action_type = ? ORDER BY id",
        (DIRECT_PAYMENT_CARD,),
    ).fetchall()
    cards: list[dict] = []
    for row in rows:
        params = json.loads(row["params_json"])
        cards.append(
            {
                "id": int(row["id"]),
                "status": str(row["status"]),
                "source": str(params.get("source") or "qbo"),
                "payment": dp.Payment(
                    ident=str(params.get("qbo_id") or ""),
                    amount_cents=int(params.get("amount_cents") or 0),
                    date=str(params.get("date") or ""),
                    check_ref=str(params.get("check_ref") or ""),
                ),
            }
        )
    return cards


def _statement_payment(line) -> dict:
    """A statement line in the review card's structured ``payments`` shape.
    Its ``qbo_id`` is the statement identity; execution swaps in the chosen
    row's own payment record (the id the auditor can verify) and keeps the
    statement identity beside it."""
    return {
        "qbo_id": str(line.statement_id),
        "amount_cents": int(line.amount_cents),
        "date": str(line.date),
        "check_ref": str(line.check_ref),
        "source": STATEMENT_UNKNOWN_SOURCE,
    }


# ---- review cards execute on approval (phase 7 row 7.1, issue #210) ---------
#
# A review card names a clearing (one payment, or a partial group) and the
# open rows that could explain it. The owner answers with ``queue approve
# --param row=<id>``; the queue refuses an approval that names no candidate,
# a settled row, or a row whose amount disagrees with the cleared total
# (amount is a confirmation field, never the discriminator: 2026-05-21), so a
# card is never approved-but-unexecutable. The next reconcile run flips the
# chosen row exactly as a clean match would, attributed to the owner.

REVIEW_CARD = "ap.reconcile_review"
REVIEW_PAID_EVENT = "ap.invoice.paid"
_CANDIDATE_ID = re.compile(r"#(\d+)\b")


def _review_candidates_text(rows: list[dict]) -> str:
    return "; ".join(
        f"#{r['id']} {r['invoice_number']} ${r['amount_cents'] / 100:,.2f}" for r in rows
    )


def _review_payment(ev) -> dict:
    """One cleared payment, structured, so execution never re-parses the
    card's display fields (the auditor's QBO lens verifies per-payment
    amounts against the accounting system, so each must be exact)."""
    return {
        "qbo_id": str(ev.qbo_id),
        "amount_cents": int(ev.amount_cents),
        "date": str(ev.date or ""),
        "check_ref": str(ev.check_ref or ""),
    }


def _review_payments(params: dict) -> list[dict]:
    """The card's cleared payments. Cards parked before 7.1 carry no
    ``payments``; a single-payment card rebuilds it from its own fields, a
    legacy group card cannot (no per-payment amounts) and yields nothing."""
    listed = params.get("payments")
    if isinstance(listed, list) and listed:
        return [
            {
                "qbo_id": str(p.get("qbo_id", "")),
                "amount_cents": int(p.get("amount_cents", 0)),
                "date": str(p.get("date", "") or ""),
                "check_ref": str(p.get("check_ref", "") or ""),
                "source": str(p.get("source", "") or ""),
            }
            for p in listed
        ]
    qbo_id = str(params.get("qbo_id", "") or "")
    if not qbo_id:
        return []
    shown = str(params.get("amount", "")).replace("$", "").replace(",", "")
    try:
        cents = amount_to_cents(Decimal(shown))
    except (ArithmeticError, ValueError):
        return []
    return [
        {
            "qbo_id": qbo_id,
            "amount_cents": cents,
            "date": str(params.get("date", "") or ""),
            "check_ref": str(params.get("check_ref", "") or ""),
        }
    ]


def _review_candidate_ids(params: dict) -> list[int]:
    return [int(m) for m in _CANDIDATE_ID.findall(str(params.get("candidates", "")))]


def check_reconcile_review(ledger, tenant: str, params: dict) -> str | None:
    """Approval-time check for a review card (``APPROVAL_CHECKS``): the
    refusal message, or None when ``row`` names an open candidate whose
    amount equals the cleared total. Also the execution-time guard, since
    the world may move between approval and the next run."""
    candidates = _review_candidate_ids(params)
    listed = str(params.get("candidates", "")) or "(none)"
    raw = str(params.get("row", "") or "").strip().lstrip("#")
    if not raw:
        return (
            f"this card needs --param row=<id> naming which open row the cleared "
            f"{params.get('amount', '')} pays; candidates: {listed}"
        )
    if not raw.isdigit() or int(raw) not in candidates:
        return f"row {raw!r} is not one of this card's candidates: {listed}"
    row = ledger.conn.execute(
        "SELECT id, invoice_number, amount_cents, status FROM ap_invoices "
        "WHERE tenant = ? AND id = ?",
        (tenant, int(raw)),
    ).fetchone()
    if row is None:
        return f"row #{raw} is not in the ledger; candidates: {listed}"
    from .status import is_settled

    if is_settled(str(row["status"])):
        return (
            f"row #{row['id']} {row['invoice_number']} is already {row['status']}; "
            "settled is terminal (reject this card if the money is explained)"
        )
    payments = _review_payments(params)
    if not payments:
        return (
            "this card was parked before review cards could execute and carries no "
            "per-payment detail; reject it (record-only, as before) and settle the row by hand"
        )
    cleared = sum(p["amount_cents"] for p in payments)
    if cleared != int(row["amount_cents"]):
        return (
            f"the cleared ${cleared / 100:,.2f} does not equal row #{row['id']} "
            f"{row['invoice_number']} ${row['amount_cents'] / 100:,.2f}; this card cannot "
            "settle a partial (leave it pending: the remainder's clearing settles the "
            "group on its own; or reject it to record the money as explained)"
        )
    return None


APPROVAL_CHECKS = {REVIEW_CARD: check_reconcile_review}


# ---- the hand-check lane (phase 7 row 7.4, issues #213 + #257) --------------
#
# A cleared payment nothing in the book explains, coded like contractor work,
# parks a card proposing the payable. Approve with the project (and the payee,
# when QBO's feed named nobody) and the next reconcile run creates the row,
# which is all the downstream 1099 machinery needs: the CPA appendix folds
# ap_invoices, so a contractor with no row is invisible to it, to lens 12, and
# to the January packet.
#
# Recording a payment the owner already made is not making one: no money moves
# here, and the engine never initiates a payment (invariant 7 is untouched).

DIRECT_PAYMENT_CARD = "ap.record_direct_payment"
DIRECT_PAYMENT_EVENT = "ap.direct_payment.recorded"

# The sweep lane (phase 7 row 7.4, issue #213), defined here because the
# cross-source memory below reads both card types: one physical check reaches
# the owner through the accounting feed, the bank statement, AND the sweep's
# note, and he answers it once.
SWEEP_CARD = "qbo.sweep_parked"
SWEEP_NOTE_EVENT = "qbo.sweep.note_read"
SWEEP_UNREADABLE = "qbo.sweep.note_unreadable"


def direct_payment_number(qbo_id: str) -> str:
    """The synthesized invoice number for a recorded direct payment.

    Derived from the QBO transaction id so it is unique per payment and
    stable across re-runs: ``insert_invoice`` keys on (tenant, vendor,
    number), so a re-executed card is a no-op rather than a second row.
    """
    return "DP-" + re.sub(r"[^A-Za-z0-9_-]+", "-", str(qbo_id)).strip("-")


def check_record_direct_payment(ledger, tenant: str, params: dict) -> str | None:
    """Approval-time check (``APPROVAL_CHECKS``): the refusal message, or None.

    Two things must be true before a row can exist, and neither is guessable:
    a payee (QBO's feed routinely names nobody) and a project (the coding
    hint is a hint; an account can carry no P-number at all). Refusing here
    rather than inventing either keeps the no-guess rule (invariant 2) and
    means a card is never approved-but-unexecutable.
    """
    payee = str(params.get("payee", "") or "").strip()
    supplied_payee = str(params.get("payee_override", "") or "").strip()
    if not (payee or supplied_payee):
        return (
            "this clearing names no payee in QBO, so approve with "
            "--param payee='<name>' (the ledger row needs somebody to belong to)"
        )
    project = str(params.get("project", "") or "").strip()
    hint = str(params.get("project_hint", "") or "").strip()
    if not (project or hint):
        return "no project could be read off the coding, so approve with --param project=P26_XXXX"
    return None


APPROVAL_CHECKS[DIRECT_PAYMENT_CARD] = check_record_direct_payment


def _approved_direct_payment_cards(ctx: JobContext) -> list[dict]:
    rows = ctx.ledger.conn.execute(
        "SELECT id, params_json FROM approval_queue "
        "WHERE action_type = ? AND status = 'approved' ORDER BY id",
        (DIRECT_PAYMENT_CARD,),
    ).fetchall()
    return [{"id": r["id"], "params": json.loads(r["params_json"])} for r in rows]


def _direct_payment_executed_ids(ctx: JobContext) -> set[str]:
    """QBO ids whose card already produced a row, from the event log."""
    done: set[str] = set()
    for e in ctx.ledger.read_event_log():
        if e.get("event_type") == DIRECT_PAYMENT_EVENT:
            qbo_id = (e.get("payload") or {}).get("qbo_id")
            if qbo_id:
                done.add(str(qbo_id))
    return done


def _direct_payment_explained(ctx: JobContext) -> set[str]:
    """QBO ids this lane has answered: any card the owner decided, approved
    or rejected. A rejection is an answer too ("this is not a payable"), and
    re-asking it every night is the nag this lane exists to avoid."""
    rows = ctx.ledger.conn.execute(
        "SELECT params_json FROM approval_queue WHERE action_type = ? AND status != 'pending'",
        (DIRECT_PAYMENT_CARD,),
    ).fetchall()
    out: set[str] = set()
    for row in rows:
        qbo_id = json.loads(row["params_json"]).get("qbo_id")
        if qbo_id:
            out.add(str(qbo_id))
    return out


DIRECT_PAYMENT_SUPERSEDED = "ap.direct_payment.superseded"


def _decide_superseded_card(ctx: JobContext, card_id: int, *, by_card: int, qbo_id: str) -> bool:
    """Mark the thin card for a physical check decided once the richer card
    for that same check has recorded it (#305).

    The engine deciding a card the owner did not is a narrow, named act, so
    it is bounded hard: only a PENDING card, only one of this lane's own,
    only the card the parked link named, and the rejection carries a note
    naming the card that answered it. The owner's queue is where cards go to
    be answered, and one physical check must not sit there twice.
    """
    row = ctx.ledger.conn.execute(
        "SELECT status, action_type FROM approval_queue WHERE tenant = ? AND id = ?",
        (ctx.tenant_slug, card_id),
    ).fetchone()
    if row is None or row["status"] != "pending" or row["action_type"] != DIRECT_PAYMENT_CARD:
        return False
    ctx.ledger.resolve_approval(
        ctx.tenant_slug,
        card_id,
        "rejected",
        param_overrides={
            "superseded_by": (
                f"card #{by_card} recorded this same physical check as {qbo_id}, "
                "with the payee and the coding the bank statement never carries"
            )
        },
    )
    return True


def _execute_direct_payment_cards(ctx: JobContext) -> tuple[list, list[str], list]:
    """Create the payable row each approved card proposes. -> events, actions, anomalies."""
    events: list = []
    actions: list[str] = []
    anomalies: list = []
    if ctx.shadow:
        return events, actions, anomalies
    already = _direct_payment_executed_ids(ctx)
    for card in _approved_direct_payment_cards(ctx):
        params = card["params"]
        qbo_id = str(params.get("qbo_id", "") or "")
        if not qbo_id or qbo_id in already:
            continue
        # The world may move between approval and execution, so the
        # approval-time check runs again here (the 7.1 pattern).
        refusal = check_record_direct_payment(ctx.ledger, ctx.tenant_slug, params)
        if refusal:
            anomalies.append(
                Anomaly(
                    code="ap.direct_payment.unexecutable",
                    detail=f"approved card #{card['id']} ({qbo_id}) cannot execute: {refusal}",
                )
            )
            continue
        vendor = str(params.get("payee_override", "") or params.get("payee", "")).strip()
        project = str(params.get("project", "") or params.get("project_hint", "")).strip()
        amount_cents = int(params.get("amount_cents", 0))
        date = str(params.get("date", "") or "")
        number = direct_payment_number(qbo_id)
        invoice_id, is_new = store.insert_invoice(
            ctx.ledger,
            tenant=ctx.tenant_slug,
            vendor=vendor,
            invoice_number=number,
            amount_cents=amount_cents,
            invoice_date=date,
            gl_account=str(params.get("gl_account", "") or ""),
            cost_type=str(params.get("cost_type", "") or ""),
            project=project,
            status="Paid",
            source_file=qbo_id,
        )
        if is_new:
            store.record_payment_details(
                ctx.ledger,
                invoice_id=invoice_id,
                payment_date=date,
                check_ref=str(params.get("check_ref", "") or ""),
            )
            store.append_note_once(
                ctx.ledger,
                invoice_id=invoice_id,
                note=(
                    f"Recorded from a cleared direct payment ({qbo_id}) on the owner's "
                    f"approval of card #{card['id']}. No invoice entered the AP lane: the "
                    "work created the obligation and the payment cleared without a "
                    "journal row, which is a recording failure, not a different "
                    "transaction (owner's call 2026-09-14)."
                ),
            )
        actions.append(f"recorded direct payment {qbo_id} as #{invoice_id} {number} ({vendor})")
        superseded = str(params.get("supersedes_card", "") or "").strip()
        if superseded.isdigit() and _decide_superseded_card(
            ctx, int(superseded), by_card=int(card["id"]), qbo_id=qbo_id
        ):
            actions.append(
                f"marked hand-check card #{superseded} decided: card #{card['id']} "
                f"recorded the same physical check ({qbo_id})"
            )
            events.append(
                EventSpec(
                    key=f"direct_payment_superseded:{superseded}",
                    event_type=DIRECT_PAYMENT_SUPERSEDED,
                    payload={
                        "card_id": int(superseded),
                        "superseded_by": int(card["id"]),
                        "qbo_id": qbo_id,
                        "invoice_id": invoice_id,
                    },
                )
            )
        events.append(
            EventSpec(
                key=f"direct_payment:{qbo_id}",
                event_type=DIRECT_PAYMENT_EVENT,
                payload={
                    "qbo_id": qbo_id,
                    "invoice_id": invoice_id,
                    "invoice_number": number,
                    "vendor": vendor,
                    "amount_cents": amount_cents,
                    "date": date,
                    "project": project,
                    "cost_type": str(params.get("cost_type", "") or ""),
                    "card_id": card["id"],
                },
            )
        )
    return events, actions, anomalies


def _approved_review_cards(ctx: JobContext) -> list[dict]:
    rows = ctx.ledger.conn.execute(
        "SELECT id, params_json FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? AND status = 'approved' ORDER BY id",
        (ctx.tenant_slug, REVIEW_CARD),
    ).fetchall()
    return [{"id": int(r["id"]), "params": json.loads(r["params_json"])} for r in rows]


def _review_executed_card_ids(ctx: JobContext) -> set[int]:
    """Execution memory: the row-level paid event names the card it answered."""
    return {
        int(e["payload"]["review_card"])
        for e in ctx.ledger.read_event_log()
        if e.get("event_type") == REVIEW_PAID_EVENT
        and str((e.get("payload") or {}).get("review_card", "")).isdigit()
    }


def _execute_review_cards(
    ctx: JobContext, rows: list[dict]
) -> tuple[list[EventSpec], list[str], list[Anomaly], int]:
    """Flip the chosen row of every approved, unexecuted review card.

    Mirrors the clean-match settle exactly (flip key ``qbo:<ids>:<row>``,
    payment date = the last clearing, check refs joined, status-lag anomaly
    on a payable row), attributed to the owner's card. The queue already
    refused a card that cannot execute; the same check runs again here
    because the world may have moved since (a later clearing settled the
    row), and a card that no longer executes is named by an anomaly, never
    silently skipped and never guessed at.
    """
    from .status import is_payable_eligible

    events: list[EventSpec] = []
    actions: list[str] = []
    anomalies: list[Anomaly] = []
    settled = 0
    executed = _review_executed_card_ids(ctx)
    by_id = {int(r["id"]): r for r in rows}
    for card in _approved_review_cards(ctx):
        if card["id"] in executed:
            continue
        params = card["params"]
        refusal = check_reconcile_review(ctx.ledger, ctx.tenant_slug, params)
        if refusal:
            anomalies.append(
                Anomaly(
                    code="ap.reconcile.review_unexecutable",
                    detail=f"approved review card #{card['id']} cannot execute: {refusal}",
                )
            )
            continue
        row = by_id[int(str(params["row"]).strip().lstrip("#"))]
        payments = _review_payments(params)
        ids = [p["qbo_id"] for p in payments]
        joined = "+".join(ids)
        pay_date = max(p["date"] for p in payments)
        refs = "+".join(p["check_ref"] for p in payments if p["check_ref"])
        dollars = row["amount_cents"] / 100
        label = f"{row['vendor']} / {row['invoice_number']} (${dollars:,.2f})"
        if is_payable_eligible(str(row["status"])):
            anomalies.append(
                Anomaly(
                    code="ap.reconcile.status_lag",
                    detail=(
                        f"{label} cleared while still {row['status']}: it was "
                        "never marked Scheduled (committed money wore a payable label)"
                    ),
                )
            )
        if ctx.shadow:
            actions.append(f"would settle {label} <- {joined} per review card #{card['id']}")
            continue
        store.update_status(
            ctx.ledger,
            invoice_id=row["id"],
            status_to="Paid",
            actor="owner:reconcile_review",
            note=f"cleared in QBO ({joined}); owner resolved review card #{card['id']}",
            flip_key=f"qbo:{joined}:{row['id']}",
        )
        store.record_payment_details(
            ctx.ledger, invoice_id=row["id"], payment_date=pay_date, check_ref=refs
        )
        statement_ids = [p["qbo_id"] for p in payments if p["source"] == STATEMENT_UNKNOWN_SOURCE]
        for p in payments:
            payload = {
                "invoice_id": row["id"],
                "vendor": row["vendor"],
                "invoice_number": row["invoice_number"],
                "amount_cents": p["amount_cents"],
                "qbo_id": p["qbo_id"],
                "payment_date": p["date"],
                "check_ref": p["check_ref"],
                "review_card": card["id"],
                "resolved_by": "owner",
            }
            if p["source"] == STATEMENT_UNKNOWN_SOURCE:
                # 7.3: the statement is the evidence; the accounting-system
                # record behind it is the chosen row's own payment (the
                # auditor verifies qbo_id there), else nothing to verify.
                payload["qbo_id"] = str(row.get("qbo_payment_id") or "")
                payload["evidence"] = STATEMENT_UNKNOWN_SOURCE
                payload["statement_id"] = p["qbo_id"]
            events.append(
                EventSpec(
                    key=f"reconciled:{p['qbo_id']}:{row['id']}",
                    event_type="ap.reconcile.paid",
                    payload=payload,
                )
            )
        row_payload = {
            "invoice_id": row["id"],
            "vendor": row["vendor"],
            "invoice_number": row["invoice_number"],
            "amount_cents": row["amount_cents"],
            "qbo_ids": ids,
            "payment_date": pay_date,
            "check_ref": refs,
            "review_card": card["id"],
            "resolved_by": "owner",
        }
        if statement_ids:
            row_payload["evidence"] = STATEMENT_UNKNOWN_SOURCE
            row_payload["statement_id"] = "+".join(statement_ids)
        events.append(
            EventSpec(
                key=f"review_paid:{card['id']}",
                event_type=REVIEW_PAID_EVENT,
                payload=row_payload,
            )
        )
        actions.append(f"settled {label} <- {joined} on {pay_date} per review card #{card['id']}")
        settled += 1
    return events, actions, anomalies, settled


def _reconcile_load(
    ctx: JobContext,
) -> tuple[
    list, list[str], list, list, VendorRegistry, list[dict], dict[str, dict], dict[int, dict]
]:
    """Everything _reconcile_run reads before it acts: the evidence window,
    the owner-explained/flagged partition, the vendor registry, the row
    snapshot, and this tenant's expense-report Purchases. Pure: no writes,
    no approvals, no events. Extracted 2026-09-18 (complexity 62 -> split),
    verbatim from _reconcile_run, so it carries that function's own inline
    comments and issue references unchanged."""
    from . import direct_payment as dp

    evidence = _RECONCILE_EVIDENCE.pop(id(ctx), None)
    if evidence is None:  # defensive: key() always ran first under the runner
        evidence = _reconcile_evidence(ctx)
    # #305: the cross-source identity reads the WHOLE feed window, before any
    # filter. Its third clause is an absence ("this number is on no other
    # feed evidence"), and an absence cannot be read off a filtered list: an
    # already-explained record still holds the number it holds.
    feed_numbers = [str(ev.check_ref) for ev in evidence if str(ev.check_ref or "").strip()]
    feed_numberless = [
        dp.Payment(
            ident=str(ev.qbo_id),
            amount_cents=int(ev.amount_cents),
            date=str(ev.date or ""),
        )
        for ev in evidence
        if not str(ev.check_ref or "").strip()
    ]
    answered, flagged_only = _reconcile_explained(ctx)
    # A card the owner already answered explains its clearing forever after,
    # whatever the feed says. Without this a payee-less payment re-cards every
    # run: the row we created carries the owner's payee, the evidence still
    # carries none, so decide() cannot connect them.
    answered |= _direct_payment_explained(ctx)
    # The engine must never read its own writing as clearing evidence
    # (write-side design rule 3): records it authored are filtered outright.
    answered |= store.engine_authored_qbo_ids(ctx.ledger, ctx.tenant_slug)
    flagged_only -= answered
    explained = answered | flagged_only
    # #293: the flagged-only ids are held aside for the hand-check lane and
    # for nothing else. They stay OUT of the loop below, exactly as they have
    # always been, so no settle, no review card and no second unknown can
    # come of a question the owner was already asked.
    flagged_only_evidence = [ev for ev in evidence if str(ev.qbo_id) in flagged_only]
    # #315: coerce here too, for consistency with the line above. QboEvidence
    # is a pydantic model with qbo_id: str, so every real construction path
    # (the live QboClient and the evidence_file JSON replay) already rejects a
    # non-str id; verified via QboEvidence.model_construct(), which is the only
    # way to bypass that and is not called anywhere in this codebase. This is
    # defense-in-depth, not a closed live bug: a future field-type change or a
    # model_construct() call elsewhere would otherwise silently reopen it.
    evidence = [ev for ev in evidence if str(ev.qbo_id) not in explained]
    registry = _vendors(ctx)
    rows = _reconcile_rows(ctx)
    # #281: the Purchases the engine wrote for this tenant's expense reports.
    # Read once; the loop below asks by exact id.
    expense_purchases = _expense_report_purchases(ctx)
    expense_reports = {int(r["id"]): r for r in expense_purchases.values()}
    return (
        evidence,
        feed_numbers,
        feed_numberless,
        flagged_only_evidence,
        registry,
        rows,
        expense_purchases,
        expense_reports,
    )


def _reconcile_policy(
    ctx: JobContext, registry: VendorRegistry
) -> tuple[set[str], list[str], int, bool, int]:
    """Every override/default reconcile reads before deciding anything:
    the ignore list, the bill-pay channel list, the out-of-scope trace
    floor, and the hand-check lane's on/off switch and its own floor. Pure:
    reads ctx.params and tenant config, writes nothing. Extracted 2026-09-18
    (complexity 62 -> split), verbatim from _reconcile_run."""
    from .registry import canonical_vendor as _canon

    # Expected recurring non-AP payees (bank fees, payroll processors): their
    # clearings are the bank rules' business, not unknown money.
    ignore_raw = ctx.params.get("ignore_payees")
    ignore_list = (
        [t for t in str(ignore_raw).split(",") if t.strip()]
        if ignore_raw is not None
        else list(ctx.tenant.qbo.reconcile_ignore_payees)
    )
    ignore_tokens = {_canon(name, registry) for name in ignore_list}
    # #285: registry payment channels whose checks the BANK writes. Empty
    # (the default) leaves the statement tier's bill-pay exception inert.
    channels_raw = ctx.params.get("bill_pay_channels")
    bill_pay_channels = (
        [t for t in str(channels_raw).split(",") if t.strip()]
        if channels_raw is not None
        else list(ctx.tenant.qbo.bill_pay_channels)
    )
    # The out-of-scope sink stops being silent at this floor (check 3048,
    # 2026-09-14): real money that falls past every rule names itself.
    floor_raw = ctx.params.get("trace_floor_cents")
    trace_floor = (
        int(str(floor_raw))
        if floor_raw is not None
        else int(ctx.tenant.qbo.reconcile_trace_floor_cents)
    )

    # ---- the hand-check lane (7.4): unexplained + contractor-shaped = a card
    dp_on_raw = ctx.params.get("direct_payment_cards")
    dp_on = (
        str(dp_on_raw).strip().lower() in ("1", "true", "yes", "on")
        if dp_on_raw is not None
        else bool(ctx.tenant.qbo.direct_payment_cards)
    )
    dp_floor_raw = ctx.params.get("direct_payment_floor_cents")
    dp_floor = (
        int(str(dp_floor_raw))
        if dp_floor_raw is not None
        else int(ctx.tenant.qbo.direct_payment_floor_cents)
    )
    return ignore_tokens, bill_pay_channels, trace_floor, dp_on, dp_floor


@dataclass
class _ReconcileOut:
    """What one reconcile run has to show for itself, accumulated by every
    tier in turn. The four lists are the JobOutput's; the five counters feed
    the summary line. One object, passed down and mutated in place, so the
    tiers share it exactly as they shared _reconcile_run's locals before the
    2026-09-21 split (complexity 42 -> under the ceiling)."""

    events: list[EventSpec] = dataclasses_field(default_factory=list)
    actions: list[str] = dataclasses_field(default_factory=list)
    anomalies: list[Anomaly] = dataclasses_field(default_factory=list)
    approvals: list[ApprovalSpec] = dataclasses_field(default_factory=list)
    settled: int = 0
    review: int = 0
    unknown: int = 0
    out_of_scope: int = 0
    already_recorded: int = 0


def _trace_out_of_scope(
    ctx: JobContext,
    out: _ReconcileOut,
    trace_floor: int,
    qbo_id,
    payee,
    amount_cents,
    date,
    check_ref,
    **extra,
) -> None:
    """One out-of-scope clearing, recorded because it is big enough to
    run down. Empty payee/check are written as empty, never omitted:
    "the feed told us nothing" is the finding."""
    if ctx.shadow or int(amount_cents) < trace_floor:
        return
    out.events.append(
        EventSpec(
            key=f"out_of_scope:{qbo_id}",
            event_type="ap.reconcile.out_of_scope",
            payload={
                "qbo_id": str(qbo_id),
                "payee": str(payee or ""),
                "amount_cents": int(amount_cents),
                "date": str(date or ""),
                "check_ref": str(check_ref or ""),
                **extra,
            },
        )
    )


def _expense_amount_drift(
    ctx: JobContext,
    out: _ReconcileOut,
    expense_reports: dict[int, dict],
    drift_recorded: set[str],
    ev,
    report_id: int,
) -> None:
    report = expense_reports.get(int(report_id))
    if report is None:
        return
    report_cents = int(report["total_cents"])
    cleared_cents = int(ev.amount_cents)
    if report_cents == cleared_cents:
        return
    token = f"{ev.qbo_id}:{report_cents}:{cleared_cents}"
    if token in drift_recorded:
        return
    drift_recorded.add(token)
    # #313, investigated 2026-09-18 and found NOT a live bug: a 2-part
    # local key could not collide across runs with different
    # report_cents, because _reconcile_key hashes report totals via
    # expense_purchases, so a changed total already produces a new
    # run_key, and the ledger's idempotency check is
    # f"{run_key}:evt:{ev.key}" -- namespaced by run_key. Verified by
    # reproduction. The key still names all three facts (2026-09-21) so
    # it reads the same as ``token`` above and as the guard set.
    delta = report_cents - cleared_cents
    note = (
        f"expense report #{report_id} holds ${report_cents / 100:,.2f} and "
        f"{ev.qbo_id} cleared ${cleared_cents / 100:,.2f} "
        f"(${abs(delta) / 100:,.2f} {'short' if delta > 0 else 'over'})"
    )
    if ctx.shadow:
        out.actions.append(f"would note an expense amount drift: {note}")
        return
    out.events.append(
        EventSpec(
            key=f"expense_drift:{ev.qbo_id}:{report_cents}:{cleared_cents}",
            event_type=EXPENSE_DRIFT_EVENT,
            payload={
                "qbo_id": str(ev.qbo_id),
                "expense_report_id": int(report_id),
                "person": str(report["person"]),
                "report_total_cents": report_cents,
                "cleared_amount_cents": cleared_cents,
                "drift_cents": delta,
                "date": str(ev.date or ""),
            },
        )
    )
    out.actions.append(note)


@dataclass
class _DirectPaymentLane:
    """The hand-check lane's planning state for one run (7.4, #305): what it
    has already asked, from either source and in any run, and the two
    cross-source maps that let one physical check ask once. Built by
    _plan_direct_payment_lane before the feed pass; ``card`` is the former
    ``_direct_payment_card`` closure of _reconcile_run, verbatim, reading the
    same state through ``self`` instead of the enclosing frame."""

    ctx: JobContext
    registry: VendorRegistry
    on: bool
    floor_cents: int
    carded: set[str]
    carded_checks: set[tuple[str, int]]
    supersedes: dict[str, int]
    numberless_cards: list
    cross_source: dict[str, str]

    def card(self, out: _ReconcileOut, ev, kind: str) -> None:
        """Park one ``ap.record_direct_payment`` card, at most once per id."""
        from . import direct_payment as dp
        from .reconcile import norm_check_ref

        ident = str(getattr(ev, "qbo_id", "") or getattr(ev, "statement_id", ""))
        if not self.on or ident in self.carded:
            return
        check = norm_check_ref(str(getattr(ev, "check_ref", "") or ""))
        instrument = (check, int(getattr(ev, "amount_cents", 0)))
        if check and instrument in self.carded_checks:
            return
        # #305: the numbered side of a pair whose other side has no number.
        # The feed card carries the payee and the coding, so it is the one
        # that asks; this line is the same money and stays silent.
        if ident in self.cross_source:
            return
        proposal = dp.propose(
            ev,
            kind,
            registry=self.registry,
            cost_types=tuple(self.ctx.tenant.qbo.direct_payment_cost_types),
            account_patterns=tuple(self.ctx.tenant.qbo.direct_payment_account_patterns),
            floor_cents=self.floor_cents,
        )
        if proposal is None:
            return
        self.carded.add(ident)
        if check:
            self.carded_checks.add(instrument)
        else:
            # #305: this card is now a candidate for the statement side,
            # in shadow too, so a dry look says what a real run would park.
            self.numberless_cards.append(
                dp.Payment(
                    ident=ident,
                    amount_cents=proposal.amount_cents,
                    date=proposal.date,
                )
            )
        source = (
            STATEMENT_UNKNOWN_SOURCE if str(getattr(ev, "txn_type", "")) == "Statement" else "qbo"
        )
        # #305: the bank asked about this same physical check first, from the
        # thin side. This card still parks, because it is the richer one and
        # the owner cannot record a payable off a statement line, but it says
        # which card it answers, and executing it decides that one too.
        superseded = self.supersedes.get(ident) if not check else None
        reason = (
            f"${proposal.amount_cents / 100:,.2f} cleared "
            f"{proposal.date or '(no date)'} to "
            f"{proposal.payee or '(no payee in the feed)'}, coded "
            f"{proposal.accounts[0] if proposal.accounts else '(no account)'}. "
            f"No ledger row explains it ({proposal.basis}). Record as an AP "
            "payable? Approve with --param project="
            + (proposal.project_hint or "P26_XXXX")
            + ("" if proposal.payee else " --param payee='<name>'")
        )
        if superseded:
            reason += (
                f" The bank statement side of this same check asked as card "
                f"#{superseded} (same amount, same day, and the feed shows that "
                "number nowhere else); approving this one answers that card too."
            )
        if self.ctx.shadow:
            # A dry look never parks a card, but it says which ones it
            # would: that is the whole point of running one before an owner
            # flips the lane on.
            out.actions.append(
                f"would park a hand-check card: {ident} "
                f"${proposal.amount_cents / 100:,.2f} "
                f"{'check ' + str(getattr(ev, 'check_ref', '')) if check else '(no check)'} "
                f"on {proposal.date} ({proposal.basis})"
            )
            return
        out.approvals.append(
            ApprovalSpec(
                key=f"direct_payment:{ident}",
                action_type=DIRECT_PAYMENT_CARD,
                params={
                    "qbo_id": proposal.qbo_id,
                    "payee": proposal.payee,
                    "amount": f"${proposal.amount_cents / 100:,.2f}",
                    "amount_cents": proposal.amount_cents,
                    "date": proposal.date,
                    "check_ref": str(getattr(ev, "check_ref", "") or ""),
                    "gl_account": proposal.accounts[0] if proposal.accounts else "",
                    "accounts": list(proposal.accounts),
                    "cost_type": proposal.cost_type,
                    "project_hint": proposal.project_hint,
                    "decision": kind,
                    "source": source,
                    **({"supersedes_card": str(superseded)} if superseded else {}),
                },
                reason=reason,
            )
        )


def _plan_direct_payment_lane(
    ctx: JobContext,
    *,
    registry: VendorRegistry,
    on: bool,
    floor_cents: int,
    feed_numbers: list[str],
    feed_numberless: list,
) -> _DirectPaymentLane:
    """Everything the lane knows before the first decision: the checks it
    already carded, and the #305 pairing of earlier statement cards against
    this run's numberless feed payments. Reads the ledger, writes nothing.
    Extracted 2026-09-21, verbatim from _reconcile_run."""
    from . import direct_payment as dp
    from .reconcile import norm_check_ref

    carded: set[str] = set()
    # Checks this lane has already put in front of the owner, in any run and
    # from either source (7.4, the second source): the accounting feed and
    # the bank statement both see the same physical check, and the owner
    # answers it once.
    carded_checks: set[tuple[str, int]] = _carded_checks(ctx)
    # #305, the second identity, for the pair that (check number, amount)
    # cannot see: one side carries no number at all.
    #
    # Held as two maps, filled from opposite directions because the two
    # sources are read at different moments in this run. The feed passes run
    # first, so a statement card can only be one an EARLIER run parked
    # (today's live shape: the bank asked first); the statement pass runs
    # last, by which time every numberless card this run parks is known.
    lane_cards = _lane_cards(ctx)
    statement_cards = [c for c in lane_cards if c["source"] == STATEMENT_UNKNOWN_SOURCE]
    # numberless feed ident -> the statement card already asking about it.
    # Paired in one call, not card by card, so two statement checks that
    # would each claim the same numberless payment cancel each other out.
    statement_card_ids = {c["payment"].ident: c["id"] for c in statement_cards}
    supersedes: dict[str, int] = {
        twin: statement_card_ids[numbered]
        for numbered, twin in dp.cross_source_pairs(
            [c["payment"] for c in statement_cards],
            feed_numberless,
            feed_numbers=feed_numbers,
        ).items()
    }
    # Numberless cards, from previous runs and from this one, as the
    # statement side's candidates. A statement line always carries a number,
    # so every card in here came from the accounting feed.
    numberless_cards: list[dp.Payment] = [
        card["payment"] for card in lane_cards if not norm_check_ref(card["payment"].check_ref)
    ]
    # statement line identity -> the numberless card that IS it. Filled in
    # the statement section below, once this run's cards are all parked.
    cross_source: dict[str, str] = {}
    return _DirectPaymentLane(
        ctx=ctx,
        registry=registry,
        on=on,
        floor_cents=floor_cents,
        carded=carded,
        carded_checks=carded_checks,
        supersedes=supersedes,
        numberless_cards=numberless_cards,
        cross_source=cross_source,
    )


def _reconcile_owner_resolutions(
    ctx: JobContext, rows: list[dict]
) -> tuple[_ReconcileOut, list[dict]]:
    """The owner's answers execute first, against the same row snapshot:
    7.1 review cards flip their chosen rows, 7.4 hand-check cards create
    theirs, so a row the owner just authorized explains its own clearing on
    this run rather than next. Their clearings are already "explained" (the
    card is not pending), so the decision loop never sees them twice.
    Returns the run's accumulator, seeded with what the cards did, and the
    snapshot to decide against. Extracted 2026-09-21 from _reconcile_run."""
    events, actions, anomalies, settled = _execute_review_cards(ctx, rows)
    dp_events, dp_actions, dp_anomalies = _execute_direct_payment_cards(ctx)
    events.extend(dp_events)
    actions.extend(dp_actions)
    anomalies.extend(dp_anomalies)
    if settled or dp_events:
        rows = _reconcile_rows(ctx)  # the snapshot after the owner's flips
    out = _ReconcileOut(events=events, actions=actions, anomalies=anomalies, settled=settled)
    return out, rows


def _reconcile_feed_pass(
    ctx: JobContext,
    out: _ReconcileOut,
    lane: _DirectPaymentLane,
    *,
    evidence: list,
    rows: list[dict],
    registry: VendorRegistry,
    expense_purchases: dict[str, dict],
    expense_reports: dict[int, dict],
    ignore_tokens: set[str],
    trace_floor: int,
) -> list:
    """The accounting feed, one clearing at a time: settle, park review,
    trace the sink, or hold an unknown for the group pass. Returns the
    ``(evidence, reason)`` pairs held back. Extracted 2026-09-21, verbatim
    from _reconcile_run; ``out`` and ``lane`` are mutated in place."""
    from .reconcile import decide, expense_report_for
    from .registry import canonical_vendor as _canon
    from .status import is_payable_eligible

    drift_recorded = _expense_drift_recorded(ctx)
    deferred_unknowns: list = []  # held for the group pass (issue #115)

    for ev in evidence:
        # The curated ignore list is an owner decision about payees, not
        # about records: an owner's own expense-report Purchase is still his
        # report's clearing and is attributed to it (#281), never swallowed.
        if (
            ev.payee
            and _canon(ev.payee, registry) in ignore_tokens
            and expense_report_for(ev, expense_purchases) is None
        ):
            out.out_of_scope += 1
            continue
        decision = decide(ev, rows, registry=registry, expense_purchases=expense_purchases)

        if decision.kind == "already_recorded":
            out.already_recorded += 1
            if decision.expense_report_id is not None:
                _expense_amount_drift(
                    ctx, out, expense_reports, drift_recorded, ev, decision.expense_report_id
                )

        elif decision.kind == "settle":
            for row in decision.rows:
                dollars = row["amount_cents"] / 100
                label = f"{row['vendor']} / {row['invoice_number']} (${dollars:,.2f})"
                if is_payable_eligible(str(row["status"])):
                    out.anomalies.append(
                        Anomaly(
                            code="ap.reconcile.status_lag",
                            detail=(
                                f"{label} cleared while still {row['status']}: it was "
                                "never marked Scheduled (committed money wore a payable label)"
                            ),
                        )
                    )
                if ctx.shadow:
                    out.actions.append(f"would settle {label} <- {ev.qbo_id} on {ev.date}")
                    continue
                store.update_status(
                    ctx.ledger,
                    invoice_id=row["id"],
                    status_to="Paid",
                    actor="engine:reconcile",
                    note=f"cleared in QBO ({ev.qbo_id})",
                    flip_key=f"qbo:{ev.qbo_id}:{row['id']}",
                )
                store.record_payment_details(
                    ctx.ledger,
                    invoice_id=row["id"],
                    payment_date=ev.date,
                    check_ref=ev.check_ref,
                )
                out.events.append(
                    EventSpec(
                        key=f"reconciled:{ev.qbo_id}:{row['id']}",
                        event_type="ap.reconcile.paid",
                        payload={
                            "invoice_id": row["id"],
                            "vendor": row["vendor"],
                            "invoice_number": row["invoice_number"],
                            "amount_cents": row["amount_cents"],
                            "qbo_id": ev.qbo_id,
                            "payment_date": ev.date,
                            "check_ref": ev.check_ref,
                        },
                    )
                )
                out.actions.append(f"settled {label} <- {ev.qbo_id} on {ev.date}")
                out.settled += 1

        elif decision.kind == "review":
            out.review += 1
            if ctx.shadow:
                out.actions.append(f"would park review: {ev.qbo_id} ({decision.reason})")
                continue
            out.events.append(
                EventSpec(
                    key=f"review:{ev.qbo_id}",
                    event_type="ap.reconcile.review_parked",
                    payload={
                        "qbo_id": ev.qbo_id,
                        "payee": ev.payee,
                        "amount_cents": ev.amount_cents,
                        "reason": decision.reason,
                    },
                )
            )
            out.approvals.append(
                ApprovalSpec(
                    key=f"reconcile:{ev.qbo_id}",
                    action_type=REVIEW_CARD,
                    params={
                        "qbo_id": ev.qbo_id,
                        "payee": ev.payee,
                        "amount": f"${ev.amount_cents / 100:,.2f}",
                        "date": ev.date,
                        "check_ref": ev.check_ref,
                        "candidates": _review_candidates_text(decision.rows),
                        "payments": [_review_payment(ev)],
                    },
                    reason=decision.reason,
                )
            )

        elif decision.kind == "unknown":
            # Not flagged yet: several partials may jointly explain one row
            # that no single payment matched (issue #115). Group pass below.
            deferred_unknowns.append((ev, decision.reason))

        else:  # out_of_scope
            out.out_of_scope += 1
            _trace_out_of_scope(
                ctx, out, trace_floor, ev.qbo_id, ev.payee, ev.amount_cents, ev.date, ev.check_ref
            )
            # The sink is where an unregistered contractor paid direct from
            # checking lands: no ledger presence and no check number, so
            # nothing above catches him.
            lane.card(out, ev, "out_of_scope")
    return deferred_unknowns


def _reconcile_group_pass(
    ctx: JobContext,
    out: _ReconcileOut,
    lane: _DirectPaymentLane,
    *,
    deferred_unknowns: list,
    rows: list[dict],
    registry: VendorRegistry,
    flagged: dict[tuple[str, int], set[str]],
) -> None:
    """The leftover unknowns, twice over: first as joint interpretations
    (several partials that together explain one row, issue #115), then one
    by one as unknown money, each named once and offered to the hand-check
    lane. Extracted 2026-09-21, verbatim from _reconcile_run."""
    from .reconcile import group_unexplained, norm_check_ref
    from .status import is_payable_eligible

    # ---- group pass: joint interpretations of the leftover unknowns ---------
    groups = group_unexplained([ev for ev, _ in deferred_unknowns], rows, registry=registry)
    grouped_ids: set[str] = set()
    for g in groups:
        ids = [str(ev.qbo_id) for ev in g.evidence]
        grouped_ids.update(ids)
        joined = "+".join(ids)
        if g.kind == "settle":
            (row,) = g.rows
            dollars = row["amount_cents"] / 100
            label = f"{row['vendor']} / {row['invoice_number']} (${dollars:,.2f})"
            if is_payable_eligible(str(row["status"])):
                out.anomalies.append(
                    Anomaly(
                        code="ap.reconcile.status_lag",
                        detail=(
                            f"{label} cleared while still {row['status']}: it was "
                            "never marked Scheduled (committed money wore a payable label)"
                        ),
                    )
                )
            if ctx.shadow:
                out.actions.append(
                    f"would settle {label} <- {joined} ({len(g.evidence)} partial payments)"
                )
                continue
            pay_date = max(str(ev.date or "") for ev in g.evidence)
            refs = "+".join(str(ev.check_ref) for ev in g.evidence if ev.check_ref)
            store.update_status(
                ctx.ledger,
                invoice_id=row["id"],
                status_to="Paid",
                actor="engine:reconcile",
                note=f"cleared in QBO as {len(g.evidence)} partial payments ({joined})",
                flip_key=f"qbo:{joined}:{row['id']}",
            )
            store.record_payment_details(
                ctx.ledger,
                invoice_id=row["id"],
                payment_date=pay_date,
                check_ref=refs,
            )
            for ev in g.evidence:
                out.events.append(
                    EventSpec(
                        key=f"reconciled:{ev.qbo_id}:{row['id']}",
                        event_type="ap.reconcile.paid",
                        payload={
                            "invoice_id": row["id"],
                            "vendor": row["vendor"],
                            "invoice_number": row["invoice_number"],
                            "amount_cents": ev.amount_cents,
                            "qbo_id": ev.qbo_id,
                            "payment_date": ev.date,
                            "check_ref": ev.check_ref,
                            "partial_group": joined,
                        },
                    )
                )
            out.actions.append(
                f"settled {label} <- {joined} on {pay_date} ({len(g.evidence)} partial payments)"
            )
            out.settled += 1
        else:  # review
            out.review += 1
            if ctx.shadow:
                out.actions.append(f"would park review: {joined} ({g.reason})")
                continue
            total = sum(int(ev.amount_cents) for ev in g.evidence)
            out.events.append(
                EventSpec(
                    key=f"review:{'+'.join(sorted(ids))}",
                    event_type="ap.reconcile.review_parked",
                    payload={
                        "qbo_ids": ",".join(sorted(ids)),
                        "payee": str(g.evidence[0].payee),
                        "amount_cents": total,
                        "reason": g.reason,
                    },
                )
            )
            out.approvals.append(
                ApprovalSpec(
                    key=f"reconcile:{'+'.join(sorted(ids))}",
                    action_type=REVIEW_CARD,
                    params={
                        "qbo_ids": ",".join(sorted(ids)),
                        "payee": str(g.evidence[0].payee),
                        "amount": f"${total / 100:,.2f}",
                        "dates": ",".join(str(ev.date) for ev in g.evidence),
                        "check_refs": ",".join(
                            str(ev.check_ref) for ev in g.evidence if ev.check_ref
                        ),
                        "candidates": _review_candidates_text(g.rows),
                        "payments": [
                            _review_payment(ev)
                            for ev in sorted(g.evidence, key=lambda e: str(e.qbo_id))
                        ],
                    },
                    reason=g.reason,
                )
            )

    for ev, reason in deferred_unknowns:
        if str(ev.qbo_id) in grouped_ids:
            continue
        if ev.check_ref and STATEMENT_UNKNOWN_SOURCE in flagged.get(
            (norm_check_ref(str(ev.check_ref)), int(ev.amount_cents)), set()
        ):
            out.already_recorded += 1  # the statement already raised this check
            lane.card(out, ev, "unknown")  # asked once, carded once (#287)
            continue
        out.unknown += 1
        out.anomalies.append(
            Anomaly(
                code="ap.reconcile.unknown_payment",
                detail=(
                    f"{ev.qbo_id}: ${ev.amount_cents / 100:,.2f} to "
                    f"{ev.payee or '(no payee)'} on {ev.date} — {reason}"
                ),
            )
        )
        if not ctx.shadow:
            out.events.append(
                EventSpec(
                    key=f"unknown:{ev.qbo_id}",
                    event_type="ap.reconcile.unknown",
                    payload={
                        "qbo_id": ev.qbo_id,
                        "payee": ev.payee,
                        "amount_cents": ev.amount_cents,
                        "date": ev.date,
                        "check_ref": ev.check_ref,
                    },
                )
            )
        # A registered contractor with a W-9 on file, paid by hand check,
        # whose payments never reached the journal: he HAS ledger presence,
        # so he lands in unknown rather than in the out-of-scope sink.
        lane.card(out, ev, "unknown")


def _reconcile_flagged_only(
    ctx: JobContext,
    out: _ReconcileOut,
    lane: _DirectPaymentLane,
    *,
    flagged_only_evidence: list,
    rows: list[dict],
    registry: VendorRegistry,
    expense_purchases: dict[str, dict],
    ignore_tokens: set[str],
) -> None:
    """Extracted 2026-09-21, verbatim from _reconcile_run."""
    from .reconcile import decide
    from .registry import canonical_vendor as _canon

    # ---- the fourth skip point (#293): feed ids flagged before the flip ----
    #
    # A payment whose only history is an unknown event was filtered out
    # above, so the lane never saw it. A CHECK survives that, because the
    # bank statement sees the same physical check and the statement side
    # already re-decides its flagged-only lines (#292, #299). A payee-less
    # non-check payment (an ACH, a debit card, a bill pay with no number)
    # has no second source at all, so the skip is permanent.
    #
    # The decision is re-run because an unknown event records what the rules
    # could see the night it was written and the rules move: the expense
    # report join (#281) and rows the owner recorded afterwards answer
    # questions that were open then. It takes NO side effect here: the id
    # counts as accounted for, nothing settles, nothing parks review, and
    # the once-only unknown is never written again. Only the lane's own
    # cardable kinds reach the lane.
    for ev in flagged_only_evidence:
        out.already_recorded += 1
        # The curated ignore list is an owner decision about payees: an
        # owner's own clearing is never a contractor's unrecorded payable.
        if ev.payee and _canon(ev.payee, registry) in ignore_tokens:
            continue
        lane.card(
            out, ev, decide(ev, rows, registry=registry, expense_purchases=expense_purchases).kind
        )


def _statement_backfill(
    ctx: JobContext, out: _ReconcileOut, line, instrument: str, rows: list[dict]
) -> None:
    """The 9066 class: the ledger recorded the money and never got the
    number, because nothing but the bank ever had it. The rows are already
    Paid, so nothing flips; the reference lands. Extracted 2026-09-21,
    verbatim from _reconcile_statement_tier."""
    ref = f"Check {line.check_ref}"
    for row in rows:
        label = f"{row['vendor']} / {row['invoice_number']}"
        if ctx.shadow:
            out.actions.append(f"would backfill {ref} onto {label} <- {instrument}")
            continue
        # A channel word ("Electronic", "ACH") is not an
        # instrument, but it was somebody's record of something,
        # so the number replaces it and the note keeps it.
        was = str(row["check_ref"] or "").strip()
        store.record_payment_details(ctx.ledger, invoice_id=row["id"], check_ref=ref)
        store.append_note_once(
            ctx.ledger,
            invoice_id=row["id"],
            note=(
                f"Check reference {ref} backfilled from the bank statement "
                f"({instrument}): the row settled without a number because "
                "nothing but the bank ever had one." + (f" (was: {was})" if was else "")
            ),
        )
        row["check_ref"] = ref  # later lines see it
        out.events.append(
            EventSpec(
                key=f"ref_backfill:{line.statement_id}:{row['id']}",
                event_type=REF_BACKFILL_EVENT,
                payload={
                    "invoice_id": row["id"],
                    "vendor": row["vendor"],
                    "invoice_number": row["invoice_number"],
                    "check_ref": ref,
                    "statement_id": line.statement_id,
                    "amount_cents": line.amount_cents,
                    "date": line.date,
                    "source": STATEMENT_UNKNOWN_SOURCE,
                },
            )
        )
        out.actions.append(f"backfilled {ref} onto {label} <- {instrument}")


def _statement_settle(
    ctx: JobContext, out: _ReconcileOut, line, instrument: str, rows: list[dict]
) -> None:
    """One statement line settles the rows it names. Mirrors the feed
    settle (flip key ``stmt:<line>:<row>``), plus the review-paid event the
    auditor verifies the clearing against. Extracted 2026-09-21, verbatim
    from _reconcile_statement_tier."""
    from .status import is_payable_eligible

    for row in rows:
        dollars = row["amount_cents"] / 100
        label = f"{row['vendor']} / {row['invoice_number']} (${dollars:,.2f})"
        if is_payable_eligible(str(row["status"])):
            out.anomalies.append(
                Anomaly(
                    code="ap.reconcile.status_lag",
                    detail=(
                        f"{label} cleared while still {row['status']}: it was "
                        "never marked Scheduled (committed money wore a payable label)"
                    ),
                )
            )
        if ctx.shadow:
            out.actions.append(f"would settle {label} <- {instrument}")
            continue
        payment_id = str(row.get("qbo_payment_id") or "")
        store.update_status(
            ctx.ledger,
            invoice_id=row["id"],
            status_to="Paid",
            actor="engine:reconcile",
            note=f"cleared on the bank statement ({instrument}, {line.statement_id})",
            flip_key=f"stmt:{line.statement_id}:{row['id']}",
        )
        store.record_payment_details(
            ctx.ledger,
            invoice_id=row["id"],
            payment_date=line.date,
            check_ref=line.check_ref,
        )
        out.events.append(
            EventSpec(
                key=f"reconciled:{line.statement_id}:{row['id']}",
                event_type="ap.reconcile.paid",
                payload={
                    "invoice_id": row["id"],
                    "vendor": row["vendor"],
                    "invoice_number": row["invoice_number"],
                    "amount_cents": row["amount_cents"],
                    # The accounting-system record behind the
                    # statement line (the auditor verifies it
                    # there); empty for a row with no engine
                    # payment, which the lens then skips.
                    "qbo_id": payment_id,
                    "payment_date": line.date,
                    "check_ref": line.check_ref,
                    "evidence": STATEMENT_UNKNOWN_SOURCE,
                    "statement_id": line.statement_id,
                },
            )
        )
        out.events.append(
            EventSpec(
                key=f"statement_paid:{line.statement_id}:{row['id']}",
                event_type=REVIEW_PAID_EVENT,
                payload={
                    "invoice_id": row["id"],
                    "vendor": row["vendor"],
                    "invoice_number": row["invoice_number"],
                    "amount_cents": row["amount_cents"],
                    "qbo_ids": [payment_id] if payment_id else [],
                    "payment_date": line.date,
                    "check_ref": line.check_ref,
                    "evidence": STATEMENT_UNKNOWN_SOURCE,
                    "statement_id": line.statement_id,
                    "resolved_by": STATEMENT_UNKNOWN_SOURCE,
                },
            )
        )
        row["status"] = "Paid"  # later lines see the settle
        row["payment_date"] = line.date
        out.actions.append(f"settled {label} <- {instrument}")
        out.settled += 1


def _reconcile_statement_tier(
    ctx: JobContext,
    out: _ReconcileOut,
    lane: _DirectPaymentLane,
    *,
    registry: VendorRegistry,
    feed_numbers: list[str],
    bill_pay_channels: list[str],
    trace_floor: int,
    flagged: dict[tuple[str, int], set[str]],
) -> list:
    """The bank's own statement files: phase 7 rows 7.3 + 7.4. Extracted
    2026-09-18 (complexity 62 -> split), verbatim from _reconcile_run; since
    2026-09-21 it writes into ``out`` and asks ``lane`` directly instead of
    taking the run's closures and threading five counters through its return.
    Returns the parsed statement lines (the summary counts them)."""
    from . import direct_payment as dp
    from .reconcile import decide_statement, norm_check_ref

    # ---- statement tier: the bank's own files (phase 7 rows 7.3 + 7.4) -----
    statement = _statement_lines(ctx)
    stmt_lines = statement.evidence
    out.actions.extend(statement.notes)
    out.anomalies.extend(statement.anomalies)
    for parsed in statement.files:
        # Informational, once per file per run: checks are the only clearing
        # evidence today, but the withdrawals and deposits were parsed and
        # tied, so the sweep and the AR lane have the bank's own counts on
        # the record without any behaviour change here.
        if not ctx.shadow:
            out.events.append(
                EventSpec(
                    key=f"statement_lines:{parsed['file']}",
                    event_type=STATEMENT_LINES_EVENT,
                    payload=dict(parsed),
                )
            )
    if stmt_lines:
        rows = _reconcile_rows(ctx)  # the snapshot after everything above flipped
        stmt_answered, stmt_flagged_only = _statement_explained(ctx)
        # #305: the statement side of the cross-source identity, computed
        # here because every numberless card this run parks is known by now.
        # Both directions must be unique: two checks written the same day for
        # the same amount cannot both be the one numberless payment, and the
        # pairing drops all of them rather than pick one.
        lane.cross_source.update(
            dp.cross_source_pairs(
                [
                    dp.Payment(
                        ident=str(line.statement_id),
                        amount_cents=int(line.amount_cents),
                        date=str(line.date),
                        check_ref=str(line.check_ref),
                    )
                    for line in stmt_lines
                ],
                lane.numberless_cards,
                feed_numbers=feed_numbers,
            )
        )
        for line in stmt_lines:
            if line.statement_id in stmt_answered:
                out.already_recorded += 1
                continue
            if line.statement_id in stmt_flagged_only:
                # #287: an unknown event is the unanswered state, not an
                # answer, so the hand-check lane gets to see the line. Its
                # counting, events and anomalies stay exactly as they were.
                #
                # The question is re-decided first (#292 follow-up), because
                # an unknown event records what the rules could see the
                # night it was written and the rules move: the legacy
                # reference tiers landed after some lines had already been
                # flagged, so a line flagged then can be answered now by a
                # settled row whose reference spells the number its own way
                # or names a two-check split. Only the lane's own cardable
                # kinds reach the lane. The decision takes NO side effect
                # here: no settle, no backfill, no review card, no event.
                # The line stays once-only, exactly as the skip has always
                # left it.
                out.already_recorded += 1
                lane.card(out, line, decide_statement(line, rows, registry=registry).kind)
                continue
            decision = decide_statement(
                line, rows, registry=registry, bill_pay_channels=bill_pay_channels
            )
            instrument = f"statement check {line.check_ref} on {line.date}"

            if decision.kind == "already_recorded":
                out.already_recorded += 1

            elif decision.kind == "ref_backfill":
                # The 9066 class: the ledger recorded the money and never got
                # the number, because nothing but the bank ever had it. The
                # row is already Paid, so nothing flips; the reference lands.
                out.already_recorded += 1
                _statement_backfill(ctx, out, line, instrument, decision.rows)

            elif decision.kind == "settle":
                _statement_settle(ctx, out, line, instrument, decision.rows)

            elif decision.kind == "review":
                out.review += 1
                if ctx.shadow:
                    out.actions.append(f"would park review: {instrument} ({decision.reason})")
                    continue
                out.events.append(
                    EventSpec(
                        key=f"review:{line.statement_id}",
                        event_type="ap.reconcile.review_parked",
                        payload={
                            "statement_id": line.statement_id,
                            "source": STATEMENT_UNKNOWN_SOURCE,
                            "check_ref": line.check_ref,
                            "amount_cents": line.amount_cents,
                            "date": line.date,
                            "reason": decision.reason,
                        },
                    )
                )
                out.approvals.append(
                    ApprovalSpec(
                        key=f"reconcile:{line.statement_id}",
                        action_type=REVIEW_CARD,
                        params={
                            "statement_id": line.statement_id,
                            "source": STATEMENT_UNKNOWN_SOURCE,
                            "payee": "",
                            "amount": f"${line.amount_cents / 100:,.2f}",
                            "date": line.date,
                            "check_ref": line.check_ref,
                            "candidates": _review_candidates_text(decision.rows),
                            "payments": [_statement_payment(line)],
                        },
                        reason=decision.reason,
                    )
                )

            elif decision.kind == "unknown":
                if (norm_check_ref(line.check_ref), line.amount_cents) in flagged:
                    out.already_recorded += 1  # QBO evidence already raised this check
                    lane.card(out, line, "unknown")  # asked once, carded once (#287)
                    continue
                out.unknown += 1
                out.anomalies.append(
                    Anomaly(
                        code="ap.reconcile.unknown_payment",
                        detail=(
                            f"{line.statement_id}: ${line.amount_cents / 100:,.2f} "
                            f"check {line.check_ref} on {line.date} on the bank statement"
                            f" ({line.description or 'no memo'}): {decision.reason}"
                        ),
                    )
                )
                if not ctx.shadow:
                    out.events.append(
                        EventSpec(
                            key=f"unknown:{line.statement_id}",
                            event_type="ap.reconcile.unknown",
                            payload={
                                "qbo_id": line.statement_id,
                                "statement_id": line.statement_id,
                                "source": STATEMENT_UNKNOWN_SOURCE,
                                "payee": "",
                                "amount_cents": line.amount_cents,
                                "date": line.date,
                                "check_ref": line.check_ref,
                                "description": line.description,
                            },
                        )
                    )
                # 7.4, the second source: the bank never loses the check
                # number, and a cleared check nothing explains is the 1099
                # escape shape. The statement carries no payee and no
                # coding, so the card asks for both.
                lane.card(out, line, "unknown")

            else:  # out_of_scope
                out.out_of_scope += 1
                _trace_out_of_scope(
                    ctx,
                    out,
                    trace_floor,
                    line.statement_id,
                    "",
                    line.amount_cents,
                    line.date,
                    line.check_ref,
                    source=STATEMENT_UNKNOWN_SOURCE,
                    description=line.description,
                )

    return stmt_lines


def _reconcile_run(ctx: JobContext) -> JobOutput:
    """Cleared money against the AP ledger, in the order the seams run:
    load, the owner's resolutions, policy, lane planning, the feed tier
    (pass, group pass, flagged-only), the statement tier, the summary. Each
    step is its own function since 2026-09-21; this is the table of contents.
    """
    (
        evidence,
        feed_numbers,
        feed_numberless,
        flagged_only_evidence,
        registry,
        rows,
        expense_purchases,
        expense_reports,
    ) = _reconcile_load(ctx)
    out, rows = _reconcile_owner_resolutions(ctx, rows)
    ignore_tokens, bill_pay_channels, trace_floor, dp_on, dp_floor = _reconcile_policy(
        ctx, registry
    )
    lane = _plan_direct_payment_lane(
        ctx,
        registry=registry,
        on=dp_on,
        floor_cents=dp_floor,
        feed_numbers=feed_numbers,
        feed_numberless=feed_numberless,
    )
    # Checks already flagged as unknown money, by either source (7.3).
    flagged = _flagged_unknown_checks(ctx)

    deferred_unknowns = _reconcile_feed_pass(
        ctx,
        out,
        lane,
        evidence=evidence,
        rows=rows,
        registry=registry,
        expense_purchases=expense_purchases,
        expense_reports=expense_reports,
        ignore_tokens=ignore_tokens,
        trace_floor=trace_floor,
    )
    _reconcile_group_pass(
        ctx,
        out,
        lane,
        deferred_unknowns=deferred_unknowns,
        rows=rows,
        registry=registry,
        flagged=flagged,
    )
    _reconcile_flagged_only(
        ctx,
        out,
        lane,
        flagged_only_evidence=flagged_only_evidence,
        rows=rows,
        registry=registry,
        expense_purchases=expense_purchases,
        ignore_tokens=ignore_tokens,
    )
    stmt_lines = _reconcile_statement_tier(
        ctx,
        out,
        lane,
        registry=registry,
        feed_numbers=feed_numbers,
        bill_pay_channels=bill_pay_channels,
        trace_floor=trace_floor,
        flagged=flagged,
    )

    if ctx.shadow:
        out.settled = sum(1 for a in out.actions if "would settle" in a)
    verb = "would settle" if ctx.shadow else "settled"
    summary = (
        f"reconcile: {verb} {out.settled}, review {out.review}, unknown {out.unknown}, "
        f"already recorded: {out.already_recorded}, out of scope: {out.out_of_scope}"
    )
    if stmt_lines:
        summary += f", statement lines: {len(stmt_lines)}"
    return JobOutput(
        status="ok",
        summary=summary,
        actions=out.actions,
        events=out.events,
        approvals=out.approvals,
        anomalies=out.anomalies,
    )


# ---------- qbo-push and qbo-push-payments (W1, W2) ---------------------
#
# Both jobs live in their own modules (public issue #2). Only the QuickBooks
# client factory stays here: evals patch ``_qbo_write_client`` on THIS module,
# so each run gets a callable that looks the name up when the write is due.


def _qbo_write_client(ctx: JobContext):
    """Factory hook: evals monkeypatch this with a fake."""
    from ...adapters.qbo import QboClient

    token_file = ctx.params.get("qbo_token_file") or ctx.tenant.qbo.token_file
    if not token_file:
        raise ValueError("no QBO token file: set [qbo].token_file in tenant.toml")
    return QboClient(token_file)


def _qbo_push_run(ctx: JobContext) -> JobOutput:
    return qbo_push.run(ctx, lambda: _qbo_write_client(ctx))


def _qbo_push_payments_run(ctx: JobContext) -> JobOutput:
    return qbo_push_payments.run(ctx, lambda: _qbo_write_client(ctx))


APPROVAL_CHECKS[qbo_push_payments.PAYMENT_CARD] = qbo_push_payments.check_payment_batch


# ---- the sweep lane (phase 7 row 7.4, issue #213) --------------------------
#
# The weekly bank-feed sweep clicks what its policy allows and parks the rest
# in a dated note under a "Needs <owner>" heading, one bullet per feed line
# with a proposal. This job reads that note back and puts each parked row in
# the approval queue, so the owner answers where every other question is
# answered instead of carrying the note in his head until next Thursday.
#
# It reads a file and parks questions. It clicks nothing, writes nothing to
# the accounting system, and moves no money: an approved card's coding is
# carried back to the NEXT sweep session as "post these" by the desk script,
# and posting it stays inside that session's own grant.


def check_qbo_sweep_parked(ledger, tenant: str, params: dict) -> str | None:
    """Approval-time check (``APPROVAL_CHECKS``): the refusal message, or None.

    A parked row is a question about coding, and the coding is exactly the
    thing the engine may not guess (invariant 2). So an approval has to carry
    the answer: the account the line should be coded to, or the existing
    record it should be matched against. Refusing here rather than storing an
    empty answer keeps a card from being approved-but-unanswerable.
    """
    account = str(params.get("account", "") or "").strip()
    match = str(params.get("match", "") or "").strip()
    if account or match:
        return None
    return (
        "a parked feed row is answered with its coding: approve with "
        "--param account='<the account it codes to>', or --param match='<the existing "
        "record it matches>' when the answer is a match rather than new coding "
        "(reject the card when the row is not the engine's business)"
    )


APPROVAL_CHECKS[SWEEP_CARD] = check_qbo_sweep_parked


def _sweep_note_dir(ctx: JobContext) -> str:
    return str(ctx.params.get("note_dir") or ctx.tenant.qbo_sweep.note_dir or "")


def _sweep_note(ctx: JobContext) -> tuple[Path | None, list[str], list[Anomaly]]:
    """(the note to read, log lines, anomalies).

    The NEWEST note is the queue. One note per sweep, and a row still waiting
    from a prior week is re-listed in it by the session's own protocol, so
    reading the history would only re-ask rows that were answered.
    """
    raw_note = str(ctx.params.get("note") or "")
    if raw_note:
        path = Path(raw_note).expanduser()
        if not path.is_file():
            return None, [f"sweep-cards: no note at {path}"], []
        return path, [], []
    raw_dir = _sweep_note_dir(ctx)
    if not raw_dir:
        return None, ["sweep-cards: no note folder configured"], []
    directory = Path(raw_dir).expanduser()
    try:
        from . import sweep_note as sn

        files = [p for p in directory.iterdir() if p.is_file() and p.name.startswith("sweep-")]
        # Dated names first, newest last: an ISO date sorts chronologically,
        # and a note somebody renamed by hand must never outrank a real one.
        dated = sorted(p for p in files if sn.note_date("", filename=p.name))
        names = dated or sorted(files)
    except FileNotFoundError:
        return None, [f"sweep-cards: no note folder at {directory}"], []
    except OSError as exc:
        # "I could not look" is never "nothing to look at" (the 2026-09-13
        # lesson): this folder lives on a tree the operating system gates per
        # program identity, and a silent empty listing reads exactly like a
        # quiet week.
        return (
            None,
            [f"sweep-cards: COULD NOT LIST {directory}"],
            [
                Anomaly(
                    code=SWEEP_UNREADABLE,
                    detail=(
                        f"could not list the sweep note folder {directory}: {exc}. This is not "
                        "'no sweep note' - the folder was not read, so any row the sweep parked "
                        "for you is invisible. Check the folder exists and that the host lets "
                        "the interpreter read it (on macOS, its Full Disk Access grant)."
                    ),
                )
            ],
        )
    if not names:
        return None, [f"sweep-cards: no sweep note in {directory}"], []
    return names[-1], [], []


def _sweep_decided_ids(ctx: JobContext) -> set[str]:
    """Feed lines this lane has answered: any card the owner decided, approved
    or rejected. A rejection is an answer too ("not the engine's business"),
    and next week's note carries the same bullet."""
    rows = ctx.ledger.conn.execute(
        "SELECT params_json FROM approval_queue WHERE action_type = ? AND status != 'pending'",
        (SWEEP_CARD,),
    ).fetchall()
    out: set[str] = set()
    for row in rows:
        line_id = json.loads(row["params_json"]).get("feed_line_id")
        if line_id:
            out.add(str(line_id))
    return out


def _sweep_cards_key(ctx: JobContext) -> str:
    """The note's bytes are the input. A new note re-fires; the same note
    replays; a folder that cannot be listed must never replay, so it takes a
    stamp and says so again every morning until somebody fixes it."""
    key = RunKey(ctx, "sweep-cards")
    key.param("note")
    key.param("note_dir")
    key.param("parked_cards")
    key.config("qbo_sweep")
    note, _, anomalies = _sweep_note(ctx)
    if anomalies:
        from datetime import UTC, datetime

        key.value("note", f"unreadable @ {datetime.now(UTC).isoformat()}")
    elif note is None:
        key.value("note", "none")
    else:
        key.value("note_name", note.name)
        key.file(note, label="note")
    return key.digest()


def _sweep_cards_run(ctx: JobContext) -> JobOutput:
    from . import sweep_note as sn
    from .reconcile import norm_check_ref

    actions: list[str] = []
    events: list[EventSpec] = []
    approvals: list[ApprovalSpec] = []

    on_raw = ctx.params.get("parked_cards")
    on = (
        str(on_raw).strip().lower() in ("1", "true", "yes", "on")
        if on_raw is not None
        else bool(ctx.tenant.qbo_sweep.parked_cards)
    )
    if not on:
        return JobOutput(
            status="ok",
            summary="sweep-cards: off ([qbo_sweep].parked_cards is false)",
        )

    note, logs, anomalies = _sweep_note(ctx)
    actions.extend(logs)
    if note is None:
        return JobOutput(
            status="ok",
            summary="sweep-cards: no sweep note to read",
            actions=actions,
            anomalies=anomalies,
        )

    text = note.read_text(encoding="utf-8", errors="replace")
    rows = sn.parse_note(text, filename=note.name)
    decided = _sweep_decided_ids(ctx)
    carded_checks = _carded_checks(ctx)

    parked = skipped = 0
    for row in rows:
        if row.line_id in decided:
            skipped += 1
            continue
        check = norm_check_ref(row.check_ref)
        if check and (check, row.amount_cents) in carded_checks:
            # The same physical check, already in front of the owner from
            # another source. One question, one answer.
            skipped += 1
            actions.append(
                f"not carding {row.amount} on {row.date or '(no date)'}: check {row.check_ref} "
                "is already in front of the owner from another source"
            )
            continue
        if check:
            carded_checks.add((check, row.amount_cents))
        if ctx.shadow:
            actions.append(
                f"would park a sweep card: {row.line_id} {row.amount} "
                f"{row.direction or '(no direction)'} on {row.date or '(no date)'} "
                f"({row.bank_text or 'no bank text'})"
            )
            parked += 1
            continue
        approvals.append(
            ApprovalSpec(
                key=f"sweep_parked:{row.line_id}",
                action_type=SWEEP_CARD,
                params={
                    "feed_line_id": row.line_id,
                    "date": row.date,
                    "amount": row.amount,
                    "amount_cents": row.amount_cents,
                    "direction": row.direction,
                    "bank_text": row.bank_text,
                    "check_ref": row.check_ref,
                    "note": note.name,
                    "parked_text": row.text[:500],
                    "account": "",
                    "source": "sweep",
                },
                reason=(
                    f"{row.amount} {row.direction or 'moved'} on {row.date or '(no date)'}"
                    f"{', ' + row.bank_text if row.bank_text else ''}"
                    f"{', check ' + row.check_ref if row.check_ref else ''}. The sweep parked it "
                    f"for you in {note.name}: {row.text[:240]}. Approve with "
                    "--param account='<the account it codes to>' (or --param match='<the "
                    "existing record>') and the next sweep note lists it under Post these."
                ),
            )
        )
        parked += 1

    if not ctx.shadow:
        events.append(
            EventSpec(
                key=f"sweep_note:{note.name}",
                event_type=SWEEP_NOTE_EVENT,
                payload={
                    "note": note.name,
                    "parked_rows": len(rows),
                    "carded": parked,
                    "already_answered": skipped,
                },
            )
        )
    return JobOutput(
        status="ok",
        summary=(
            f"sweep-cards: {note.name}, {len(rows)} parked row(s), "
            f"{parked} card(s), {skipped} already answered"
        ),
        actions=actions,
        events=events,
        approvals=approvals,
        anomalies=anomalies,
    )


JOBS: dict[str, JobHandler] = {
    "intake": JobHandler(key=_intake_key, run=_intake_run),
    "verify-payment": JobHandler(key=_verify_key, run=_verify_run),
    "queue-status": JobHandler(key=_queue_status_key, run=_queue_status_run),
    "apply": JobHandler(key=_apply_key, run=_apply_run),
    "workbook": JobHandler(key=_workbook_key, run=_workbook_run),
    "dismiss": JobHandler(key=_dismiss_key, run=_dismiss_run),
    "identify": JobHandler(key=_identify_key, run=_identify_run),
    "janitor": JobHandler(key=_janitor_key, run=_janitor_run),
    "reconcile": JobHandler(key=_reconcile_key, run=_reconcile_run),
    "qbo-push": JobHandler(key=qbo_push.run_key, run=_qbo_push_run),
    "qbo-push-payments": JobHandler(key=qbo_push_payments.run_key, run=_qbo_push_payments_run),
    "sweep-cards": JobHandler(key=_sweep_cards_key, run=_sweep_cards_run),
}


# What deciding each card is, for a tenant with authority.toml (#435;
# core.authority.CardRule). A tenant without one never reads this.
CARD_AUTHORITY: dict[str, CardRule] = {
    "ap.review_needs_ocr": CardRule("approve", "ap.invoice"),
    "ap.review_incomplete_extraction": CardRule("approve", "ap.invoice"),
    "ap.review_revised_invoice": CardRule("approve", "ap.invoice", amount="new_amount"),
    NEW_VENDOR_CARD: CardRule("approve", "vendor", money=False),
    "ap.w9_file_and_flip": CardRule("approve", "vendor", money=False),
    "ap.payment_recommendation": CardRule("approve", "payment", amount="amount"),
    REVIEW_CARD: CardRule("approve", "payment", amount="amount"),
    DIRECT_PAYMENT_CARD: CardRule("approve", "payment", amount="amount_cents", cents=True),
    SWEEP_CARD: CardRule("approve", "books", amount="amount_cents", cents=True),
    "ap.qbo_push_batch": CardRule("approve", "books"),
    "ap.qbo_payment_batch": CardRule("approve", "books"),
    "ap.qbo_map_vendor": CardRule("approve", "books", money=False),
    "ap.qbo_map_account": CardRule("approve", "books", money=False),
    "ap.qbo_duplicate_review": CardRule("approve", "books", money=False),
}
