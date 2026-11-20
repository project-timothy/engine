"""The inbox job (issue #112): files in the Taildrop receipt inbox are
classified, then filed as receipts or skipped, each on its card.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobOutput
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from . import inbox as inbox_boundary
from .common import _approved_cards, _drop_dir, _events_of, _sha256, _sha256_or_none
from .schema import (
    RECEIPT_SUFFIXES,
)

# Issue #112: the Taildrop receipt-inbox pre-stage.
INBOX_CLASSIFIED_EVENT = "expense.inbox_classified"
INBOX_FILED_EVENT = "expense.inbox_filed"
INBOX_SKIPPED_EVENT = "expense.inbox_skipped"
INBOX_FILE_CARD = "expenses.inbox_file_receipt"
INBOX_SKIP_CARD = "expenses.inbox_skip"
INBOX_FILED_RECORD = "expense.inbox_filed.recorded"
INBOX_SKIPPED_RECORD = "expense.inbox_skipped.recorded"


def _pending_skip_cards(ctx: JobContext) -> list[dict]:
    """This lane's own pending skip cards, each with the ApprovalSpec key it was
    parked under (the subject key's tail), the shape skip_card_supersessions reads."""
    prefix = f"apr:{ctx.tenant_slug}:expenses:{INBOX_SKIP_CARD}:"
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM approval_queue WHERE tenant = ? AND action_type = ? AND status = 'pending'",
        (ctx.tenant_slug, INBOX_SKIP_CARD),
    ).fetchall()
    out = []
    for r in rows:
        subject = str(r["idempotency_key"])
        if not subject.startswith(prefix):
            continue
        out.append(
            {
                "id": r["id"],
                "action_type": r["action_type"],
                "status": r["status"],
                "key": subject[len(prefix) :],
                "params": json.loads(r["params_json"] or "{}"),
            }
        )
    return out


def skip_card_supersessions(cards: list[dict], shas) -> list[dict]:
    """Which pending skip cards tonight's skip card replaces (proposal 99e8ae57).

    Containment, never recency: a card is superseded only when every file it
    asks about is in tonight's set AND tonight's set is strictly larger (an
    equal set is the same key, which the queue's dedup already handles). A
    card holding one file tonight lacks, a card of another action type, and an
    answered card are all left alone. Oldest first."""
    tonight = set(shas)
    plan = []
    for card in sorted(cards, key=lambda c: c["id"]):
        if card.get("action_type") != INBOX_SKIP_CARD or card.get("status") != "pending":
            continue
        theirs = {s for s in str(card.get("params", {}).get("sha256s", "")).split(",") if s}
        if theirs and theirs < tonight:
            plan.append({"card_id": card["id"], "key": card["key"]})
    return plan


# ---- job: inbox (issue #112) ------------------------------------------------


def _inbox_dir(ctx: JobContext) -> Path | None:
    configured = ctx.params.get("inbox_dir") or ctx.tenant.expenses.inbox_dir
    return Path(configured).expanduser() if configured else None


def _inbox_person(ctx: JobContext) -> str:
    return str(ctx.params.get("inbox_person") or ctx.tenant.expenses.inbox_person or "")


def _inbox_candidates(ctx: JobContext) -> list[Path]:
    """Images at the inbox ROOT only: ``_not-receipts/`` and any other
    subfolder are out of scope (skips live there; never deleted)."""
    inbox = _inbox_dir(ctx)
    if inbox is None or not inbox.is_dir():
        return []
    return sorted(
        p
        for p in inbox.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and not p.name.endswith(inbox_boundary.LABEL_SIDECAR_SUFFIX)
        and p.suffix.lower() in RECEIPT_SUFFIXES
    )


def _inbox_events_by_sha(ctx: JobContext, event_type: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in _events_of(ctx, event_type):
        payload = e.get("payload", {})
        sha = str(payload.get("sha256", ""))
        if sha:
            out[sha] = payload
    return out


def _inbox_records_by_sha(ctx: JobContext, record_type: str) -> dict[str, dict]:
    """Job records (03-F9) by content hash: the memory of a move a dying run
    made after recording it but before its event landed."""
    out: dict[str, dict] = {}
    for rec in ctx.records(record_type):
        sha = str(rec["payload"].get("sha256", ""))
        if sha:
            out[sha] = rec["payload"]
    return out


def _classifier_kind(ctx: JobContext) -> str:
    return str(ctx.params.get("classifier") or "claude")


def _inbox_key(ctx: JobContext) -> str:
    """Inbox content, config, classifier, and card RESOLUTIONS are all run
    inputs: an approval must re-fire execution instead of replaying the
    propose-only result (the issue #119/#121 replay lesson)."""
    key = RunKey(ctx, "expinbox")
    key.add("inbox_dir", str(_inbox_dir(ctx) or ""))
    key.add("person", _inbox_person(ctx))
    key.add("classifier", str(key.param("classifier") or "claude"))
    key.param("drop_dir")
    key.config("expenses")
    # Row 7.11: classification runs through the gateway, so the resolved
    # inbox_classify tier rides the key (docs/run-keys.md).
    key.config("llm")
    key.value("llm_tier", inbox_boundary.resolved_tier(ctx, _classifier_kind(ctx)))
    key.value("candidates", sorted(f"{p.name}:{_sha256(p)[:16]}" for p in _inbox_candidates(ctx)))
    cards = [
        f"card:{c['id']}:{c['params'].get('project', '')}:{c['params'].get('person', '')}"
        for action in (INBOX_FILE_CARD, INBOX_SKIP_CARD)
        for c in _approved_cards(ctx, action)
    ]
    key.value("cards", cards)
    return key.digest()


def _inbox_run(ctx: JobContext) -> JobOutput:  # noqa: C901  # complexity 25, tracked debt
    inbox = _inbox_dir(ctx)
    if inbox is None:
        return JobOutput(status="ok", summary="expenses inbox: no inbox_dir configured")
    if not inbox.is_dir():
        return JobOutput(status="ok", summary=f"expenses inbox: {inbox} does not exist")
    events: list[EventSpec] = []
    approvals: list[ApprovalSpec] = []
    actions: list[str] = []
    anomalies: list[Anomaly] = []
    drop = _drop_dir(ctx)

    candidates = _inbox_candidates(ctx)
    by_sha = {_sha256(p): p for p in candidates}
    filed_done = _inbox_events_by_sha(ctx, INBOX_FILED_EVENT)
    skipped_done = _inbox_events_by_sha(ctx, INBOX_SKIPPED_EVENT)
    filed_records = _inbox_records_by_sha(ctx, INBOX_FILED_RECORD)
    skip_records = _inbox_records_by_sha(ctx, INBOX_SKIPPED_RECORD)

    # -- execute approved decisions first (this run's files may then flow) --
    # Record-then-move (03-F9): the job record is durable BEFORE the move,
    # so a run that dies between the two leaves a record the next run turns
    # into the event (an action, no anomaly). A moved file with NEITHER
    # record nor event (a hand move, or a run from before records existed)
    # is found where the card said it would go, hash-verified, and recorded
    # with an anomaly instead of being read as "already handled".
    moved = recovered = 0
    for card in _approved_cards(ctx, INBOX_FILE_CARD):
        sha = str(card["params"].get("sha256", ""))
        if sha in filed_done:
            continue
        path = by_sha.get(sha)
        if drop is None:
            if path is not None:
                anomalies.append(
                    Anomaly(
                        code="expenses.inbox_no_drop_dir",
                        detail="approved inbox receipt but [expenses].drop_dir is unset",
                    )
                )
            continue
        person = str(card["params"].get("person") or _inbox_person(ctx))
        project = str(card["params"].get("project", "")).strip()
        # No project on the card -> the person's ROOT: the existing intake
        # attribution card (card-never-guess) takes over from there.
        dest_dir = drop / person / project if project else drop / person
        name = path.name if path is not None else str(card["params"].get("file", ""))
        dest = dest_dir / name
        if path is None:
            # Gone from the inbox with no filing on record: a run recorded the
            # move and died before the event (03-F9, healed from the record),
            # or the human moved it by hand / a pre-record run died (found by
            # hash, recorded with an anomaly).
            if name and dest.is_file() and _sha256_or_none(dest) == sha:
                recovered += 1
                recorded = filed_records.get(sha)
                if recorded is not None:
                    actions.append(
                        f"recorded the filing of {name} from its job record "
                        "(a run died between the move and the record)"
                    )
                else:
                    anomalies.append(
                        Anomaly(
                            code="expenses.inbox_filing_unrecorded",
                            detail=f"{name}: found at {dest_dir} with no expense.inbox_filed "
                            "record (a run failed between the move and the record); "
                            "recorded now",
                        )
                    )
                events.append(
                    EventSpec(
                        key=f"inboxfiled:{sha[:16]}",
                        event_type=INBOX_FILED_EVENT,
                        payload={"sha256": sha, "file": name, "dest": str(dest)},
                    )
                )
            continue
        if ctx.shadow:
            actions.append(f"would file {path.name} -> {dest_dir}")
            continue
        ctx.guard.check_write(dest)
        dest_dir.mkdir(parents=True, exist_ok=True)
        payload = {"sha256": sha, "file": path.name, "dest": str(dest)}
        ctx.record_now(f"inboxfiled:{sha[:16]}", INBOX_FILED_RECORD, payload)  # 03-F9
        shutil.move(str(path), dest)
        moved += 1
        actions.append(f"filed {path.name} -> {dest_dir}")
        events.append(
            EventSpec(key=f"inboxfiled:{sha[:16]}", event_type=INBOX_FILED_EVENT, payload=payload)
        )
    skipped = 0
    not_receipts = inbox / "_not-receipts"
    parked_by_sha: dict[str, Path] | None = None  # _not-receipts/, hashed on demand
    for card in _approved_cards(ctx, INBOX_SKIP_CARD):
        for sha in str(card["params"].get("sha256s", "")).split(","):
            sha = sha.strip()
            if not sha or sha in skipped_done:
                continue
            path = by_sha.get(sha)
            if path is None:
                # Same shape as the filer: a skip moved and never recorded.
                if parked_by_sha is None:
                    parked_by_sha = (
                        {_sha256(p): p for p in sorted(not_receipts.iterdir()) if p.is_file()}
                        if not_receipts.is_dir()
                        else {}
                    )
                parked = parked_by_sha.get(sha)
                if parked is not None:
                    recovered += 1
                    if sha in skip_records:
                        actions.append(
                            f"recorded the skip of {parked.name} from its job record "
                            "(a run died between the move and the record)"
                        )
                    else:
                        anomalies.append(
                            Anomaly(
                                code="expenses.inbox_skip_unrecorded",
                                detail=f"{parked.name}: found in _not-receipts/ with no "
                                "expense.inbox_skipped record (a run failed between the "
                                "move and the record); recorded now",
                            )
                        )
                    events.append(
                        EventSpec(
                            key=f"inboxskipped:{sha[:16]}",
                            event_type=INBOX_SKIPPED_EVENT,
                            payload={"sha256": sha, "file": parked.name},
                        )
                    )
                continue
            dest = not_receipts / path.name
            if ctx.shadow:
                actions.append(f"would move {path.name} -> _not-receipts/")
                continue
            ctx.guard.check_write(dest)
            not_receipts.mkdir(parents=True, exist_ok=True)
            payload = {"sha256": sha, "file": path.name}
            ctx.record_now(f"inboxskipped:{sha[:16]}", INBOX_SKIPPED_RECORD, payload)  # 03-F9
            shutil.move(str(path), dest)
            skipped += 1
            actions.append(f"moved {path.name} -> _not-receipts/ (never deleted)")
            events.append(
                EventSpec(
                    key=f"inboxskipped:{sha[:16]}", event_type=INBOX_SKIPPED_EVENT, payload=payload
                )
            )

    # -- classify what remains unlabelled (event memory, one LLM look ever) --
    classified = _inbox_events_by_sha(ctx, INBOX_CLASSIFIED_EVENT)
    classifier = inbox_boundary.build_classifier(_classifier_kind(ctx), ctx)
    new_receipts = 0
    new_skips: list[tuple[str, str]] = []  # (sha, name)
    for path in _inbox_candidates(ctx):
        sha = _sha256(path)
        if sha in filed_done or sha in skipped_done:
            continue
        prior = classified.get(sha)
        if prior is None:
            label = inbox_boundary.sanitize(classifier.classify(path))
            payload = {
                "sha256": sha,
                "file": path.name,
                "receipt": label.receipt,
                "confidence": label.confidence,
            }
            if label.receipt:
                payload.update(
                    vendor=label.vendor, amount=label.amount, expense_date=label.expense_date
                )
            events.append(
                EventSpec(
                    key=f"inboxcls:{sha[:16]}",
                    event_type=INBOX_CLASSIFIED_EVENT,
                    payload=payload,
                )
            )
            prior = payload
        if prior.get("receipt"):
            new_receipts += 1
            approvals.append(
                ApprovalSpec(
                    key=f"inboxfile:{sha[:16]}",
                    action_type=INBOX_FILE_CARD,
                    params={
                        "file": path.name,
                        "sha256": sha,
                        "person": _inbox_person(ctx),
                        "vendor": str(prior.get("vendor", "")),
                        "amount": str(prior.get("amount", "")),
                        "expense_date": str(prior.get("expense_date", "")),
                        "confidence": str(prior.get("confidence", "")),
                    },
                    reason="inbox image classified as a receipt; approve with "
                    "--param project=P.. (and optionally person=..) to file it "
                    "into the drop tree — without a project it lands at the "
                    "person root and normal attribution takes over",
                )
            )
        else:
            new_skips.append((sha, path.name))

    superseded: list[dict] = []
    if new_skips:
        digest = hashlib.sha256(",".join(sorted(s for s, _ in new_skips)).encode()).hexdigest()
        # Each unanswered night's set contains the last one, so the older card
        # retires instead of waiting to be answered by hand (proposal 99e8ae57).
        superseded = skip_card_supersessions(_pending_skip_cards(ctx), [s for s, _ in new_skips])
        approvals.append(
            ApprovalSpec(
                key=f"inboxskip:{digest[:16]}",
                action_type=INBOX_SKIP_CARD,
                params={
                    # Label-only contract: filenames and count, no content.
                    "count": str(len(new_skips)),
                    "files": "; ".join(sorted(n for _, n in new_skips)),
                    "sha256s": ",".join(sorted(s for s, _ in new_skips)),
                },
                reason="classified as not receipts; approve to move them to "
                "_not-receipts/ (kept, never deleted)",
                supersedes_keys=[entry["key"] for entry in superseded],
            )
        )

    return JobOutput(
        status="ok",
        summary=(
            f"expenses inbox: {new_receipts} receipt proposal(s), "
            f"{len(new_skips)} skip proposal(s), filed {moved}, moved {skipped} to "
            "_not-receipts"
        )
        + (f", new skip card covers {len(superseded)} older pending one(s)" if superseded else "")
        + (
            f", recorded {recovered} move(s) a dying run left without an event" if recovered else ""
        ),
        events=events,
        approvals=approvals,
        actions=actions,
        anomalies=anomalies,
    )
