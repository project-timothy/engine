"""The match job: an approved, reimbursed report is recorded in QBO as a
Purchase, approval-gated, duplicate-guarded, and readback-verified (the W1
contract). The engine never moves money (invariant 7).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ...engine import clock
from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobOutput
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from .common import _approved_cards, _events_of, _now, _person_named, _persons
from .confirm_card import CONFIRM_CARD, _confirm_card_state, _confirm_spec, _lines_for, _reports
from .records import (
    STATUS_OPEN,
    STATUS_RECORDED,
    STATUS_REIMBURSED,
)


def _payee_name(ctx: JobContext, person_name: str) -> str:
    """The accounting-system payee a report books to: the vendor record
    behind a vendor-role person, else the person's own name."""
    person = _person_named(ctx, person_name)
    return (getattr(person, "qbo_vendor", "") if person else "") or person_name


def _project_accounts(
    template: str, projects: list[str], account_map: dict[str, str]
) -> tuple[dict[str, str], list[str]]:
    """Resolve projects to chart account ids via the tenant template
    (vendor-role booking, issue #120): ``{number}`` is the project code
    minus its letter prefix (P26_2001 -> 26_2001). Anything unresolvable
    lands in ``missing`` — a card, never a guess, never a fallback to the
    per-category table."""
    ids: dict[str, str] = {}
    missing: list[str] = []
    for project in projects:
        number = re.sub(r"^[A-Za-z]+", "", str(project or ""))
        fqn = template.replace("{number}", number) if template and number else ""
        acct = account_map.get(fqn) if fqn else None
        if acct is None:
            missing.append(project or "(no project)")
        else:
            ids[project] = acct
    return ids, missing


# ---- job: match -------------------------------------------------------------


def _qbo_write_client(ctx: JobContext):
    """Factory hook: evals monkeypatch this with a fake."""
    from ...adapters.qbo import QboClient

    token_file = ctx.params.get("qbo_token_file") or ctx.tenant.qbo.token_file
    if not token_file:
        raise ValueError("no QBO token file: set [qbo].token_file in tenant.toml")
    return QboClient(token_file)


def _confirmed_report_ids(ctx: JobContext) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for card in _approved_cards(ctx, CONFIRM_CARD):
        rid = card["params"].get("report_id", "")
        if str(rid).isdigit():
            out[int(rid)] = card
    return out


# Bumped when match semantics change, so a code fix re-runs against the same
# inputs instead of replaying the prior result — the PR #62 / issue #115
# gap-3 lesson applied to code changes, not just config changes. v2:
# issue #133 (leg-3 window anchors to the report month; line consumption).
MATCH_VERSION = "2"


def _match_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expmatch", version=MATCH_VERSION)
    key.value("open", [f"{r['id']}:{r['status']}" for r in _reports(ctx, STATUS_OPEN)])
    key.value("recorded", [f"{r['id']}:{r['status']}" for r in _reports(ctx, STATUS_RECORDED)])
    unverified = [f"{r['id']}:{r['qbo_purchase_id']}" for r in _unverified_reports(ctx)]
    key.value("unverified", unverified)
    if unverified:
        key.value("verify_day", _verify_day(ctx))
    key.value("confirmed", sorted(str(i) for i in _confirmed_report_ids(ctx)))
    # 03-F7: the newest confirm card's state per open report is a run input.
    # A reject with nothing else changing must re-fire, or the re-ask (a
    # fresh key naming the rejected card) can never be parked.
    key.value(
        "confirm_cards",
        [
            f"{r['id']}:{state}:{card_id}"
            for r in _reports(ctx, STATUS_OPEN)
            for state, card_id in (_confirm_card_state(ctx, int(r["id"])),)
        ],
    )
    # Issue #115 gap 3: the category mapping is a run input — a config fix
    # must produce a fresh key, or a parked report replays parked forever.
    # The same holds for the vendor-role inputs (issue #120). The #153 audit
    # added the rest of the section (window, default channel) and the bank
    # CSV format: the whole section, uniformly.
    key.config("expenses", "bank_csv", "qbo", "identity.timezone", "close.bank_account")
    csv = str(key.param("bank_csv") or "")
    if csv and Path(csv).is_file():
        key.file(csv, label="bank_csv")
    return key.digest()


def _purchase_payload(
    report: dict,
    lines: list[dict],
    *,
    payee_id: str,
    bank_account_id: str,
    account_ids: dict[str, str],
    txn_date: str,
    group_by: str = "category",
) -> dict:
    # Owner/employee reports split one line per category; a vendor-role
    # report splits one line per project (issue #120) — same machinery,
    # different grouping key, account_ids keyed to match.
    by_key: dict[str, int] = {}
    for line in lines:
        by_key[line[group_by]] = by_key.get(line[group_by], 0) + line["amount_cents"]
    return {
        "PaymentType": "Check",
        "AccountRef": {"value": bank_account_id},
        "EntityRef": {"value": payee_id, "type": "Vendor"},
        "TxnDate": txn_date or None,
        "DocNumber": f"EXP-{report['id']}"[:21],
        "PrivateNote": f"engine:{report['idempotency_key']}",
        "Line": [
            {
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": round(cents / 100, 2),
                "Description": f"{report['person']} expense report {report['month']} — {key}",
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account_ids[key]}},
            }
            for key, cents in sorted(by_key.items())
        ],
    }


_REPORT_FIELDS = frozenset(
    {"qbo_purchase_id", "reimbursed_channel", "instrument_ref", "reimbursed_date", "cleared_date"}
)


def _sets(fields: dict) -> str:
    """``col = ?`` for each field. A column name is interpolated into the SQL,
    so only expense_report's settable columns may arrive; values stay ? marks."""
    unknown = set(fields) - _REPORT_FIELDS
    if unknown:
        raise ValueError(f"expense_report has no settable {sorted(unknown)}")
    return ", ".join(f"{k} = ?" for k in fields)


def _set_report_status(ctx: JobContext, report_id: int, status: str, **fields) -> None:
    sets = _sets(fields)
    sql = f"UPDATE expense_report SET status = ?, updated_at = ?{', ' + sets if sets else ''} "  # noqa: S608  (columns from _REPORT_FIELDS only)
    sql += "WHERE id = ?"
    ctx.ledger.conn.execute(sql, (status, _now(), *fields.values(), report_id))
    ctx.ledger.conn.commit()


def _update_report(ctx: JobContext, report_id: int, **fields) -> None:
    """Field update that leaves the status alone."""
    sets = _sets(fields)
    ctx.ledger.conn.execute(
        f"UPDATE expense_report SET updated_at = ?, {sets} WHERE id = ?",  # noqa: S608  (columns from _REPORT_FIELDS only)
        (_now(), *fields.values(), report_id),
    )
    ctx.ledger.conn.commit()


def _unverified_reports(ctx: JobContext) -> list[dict]:
    """Open reports that already carry a Purchase id: written, not yet read
    back right (honesty audit 2026-09-03, 03-F1)."""
    return [r for r in _reports(ctx, STATUS_OPEN) if r["qbo_purchase_id"]]


def _verify_day(ctx: JobContext) -> str:
    """The tenant-local date. A match-key input only while an unverified
    Purchase exists, so the verify pass retries once a day instead of
    replaying a transport failure forever."""
    return clock.local_date(_now(), ctx.tenant.identity.timezone)


def _own_purchase_ids(ctx: JobContext) -> set[str]:
    """Every Purchase id the engine wrote, verified or not (W1 rule 3: never
    read our own writing as evidence, and never mistake it for the owner's
    hand-coding)."""
    rows = ctx.ledger.conn.execute(
        "SELECT qbo_purchase_id FROM expense_report WHERE tenant = ? "
        "AND qbo_purchase_id IS NOT NULL AND qbo_purchase_id != ''",
        (ctx.tenant_slug,),
    ).fetchall()
    return {str(r["qbo_purchase_id"]) for r in rows}


def _readback_purchase(client, purchase_id: str, total_cents: int) -> tuple[bool, str]:
    """Read a Purchase back and compare the total. Returns (verified, note).
    Never raises: the Purchase exists whatever the readback does, and the
    caller has already remembered its id."""
    expected = round(total_cents / 100, 2)
    try:
        readback = client.get_purchase(purchase_id)
    except Exception as exc:  # any transport failure, not only the API's own errors
        return False, f"readback failed: {exc}"
    got = readback.get("TotalAmt")
    try:
        if round(float(got), 2) == expected:
            return True, "readback ok"
    except (TypeError, ValueError):
        pass
    return False, f"readback {got!r}, expected {expected}"


def _complete_record(ctx: JobContext, report: dict, card: dict, purchase_id: str) -> None:
    """The verified write becomes the record: status flips to Recorded with
    the card's provenance (channel, instrument, tenant-local booked date)."""
    tz = ctx.tenant.identity.timezone
    _set_report_status(
        ctx,
        report["id"],
        STATUS_RECORDED,
        qbo_purchase_id=purchase_id,
        reimbursed_channel=str(card.get("params", {}).get("channel", "")),
        instrument_ref=str(card.get("params", {}).get("instrument_ref", "")),
        reimbursed_date=clock.local_date(str(card.get("resolved_at") or ""), tz),
    )


def _readback_anomaly(report_id: int, purchase_id: str, note: str) -> Anomaly:
    code = (
        "expenses.qbo_readback_failed"
        if note.startswith("readback failed")
        else "expenses.qbo_readback_mismatch"
    )
    return Anomaly(
        code=code,
        detail=f"purchase {purchase_id} for report #{report_id}: {note}; id kept on the "
        "report (Open, unverified), retried daily; stopped",
    )


def _match_run(ctx: JobContext) -> JobOutput:  # noqa: C901  # complexity 41, tracked debt
    events: list[EventSpec] = []
    approvals: list[ApprovalSpec] = []
    anomalies: list[Anomaly] = []
    actions: list[str] = []

    open_reports = _reports(ctx, STATUS_OPEN)
    confirmed = _confirmed_report_ids(ctx)
    window = timedelta(days=ctx.tenant.expenses.match_window_days)

    # -- leg 1: record confirmed reimbursements in QBO ------------------------
    to_record = [r for r in open_reports if r["id"] in confirmed and not r["qbo_purchase_id"]]
    if to_record and ctx.shadow:
        for r in to_record:
            actions.append(f"would record report #{r['id']} (${r['total_cents'] / 100:,.2f})")
        to_record = []
    # parked = a card the owner can clear; blocked = an anomaly that parked
    # nothing (03-F12): the two used to share one counter, so "parked 1"
    # stood over an empty approvals list.
    recorded = parked = blocked = unverified = 0
    client = None

    # -- leg 1a: verify Purchases written but not yet read back right ---------
    # (honesty audit 2026-09-03, 03-F1). The id was remembered at create time;
    # a good readback now completes the record, a bad one flags again.
    to_verify = [r for r in open_reports if r["qbo_purchase_id"]] if not ctx.shadow else []
    for report in to_verify:
        client = client or _qbo_write_client(ctx)
        purchase_id = str(report["qbo_purchase_id"])
        ok, note = _readback_purchase(client, purchase_id, report["total_cents"])
        if not ok:
            unverified += 1
            anomalies.append(_readback_anomaly(report["id"], purchase_id, note))
            continue
        _complete_record(ctx, report, confirmed.get(report["id"], {}), purchase_id)
        recorded += 1
        actions.append(f"verified report #{report['id']} -> Purchase {purchase_id}")
        events.append(
            EventSpec(
                key=f"exppurchase-verified:{report['idempotency_key']}",
                event_type="expense.qbo_purchase_verified",
                payload={"report_id": report["id"], "qbo_purchase_id": purchase_id},
            )
        )

    if to_record:
        from ...adapters.qbo import QboApiError

        client = client or _qbo_write_client(ctx)
        vendor_map = {
            str(v["display_name"]).strip().lower(): v["id"] for v in client.fetch_vendors()
        }
        account_map = {a["fully_qualified_name"]: a["id"] for a in client.fetch_accounts()}
        tz = ctx.tenant.identity.timezone
        since = min(
            clock.local_date(str(confirmed[r["id"]].get("resolved_at") or _now()), tz)
            for r in to_record
        )
        recent = client.fetch_recent_txns(since=since)
        own_ids = _own_purchase_ids(ctx)
        for report in to_record:
            card = confirmed[report["id"]]
            # The approval instant is stored UTC; the booked date is a
            # tenant-calendar fact. An evening approval must not post the
            # reimbursement to UTC's next day (#136).
            txn_date = clock.local_date(str(card.get("resolved_at") or ""), tz)
            payee_name = _payee_name(ctx, report["person"])
            payee_id = vendor_map.get(payee_name.strip().lower())
            if payee_id is None:
                parked += 1
                approvals.append(
                    ApprovalSpec(
                        key=f"expmap:payee:{report['id']}",
                        action_type="expenses.qbo_map_payee",
                        params={
                            "report_id": str(report["id"]),
                            "person": report["person"],
                            "payee": payee_name,
                        },
                        reason="no accounting-system payee matches this person; create "
                        "or map the vendor record",
                    )
                )
                continue
            bank_name = ctx.tenant.close.bank_account
            bank_id = account_map.get(bank_name)
            if bank_id is None:
                blocked += 1
                anomalies.append(
                    Anomaly(
                        code="expenses.qbo_bank_unmapped",
                        detail=f"[close].bank_account {bank_name!r} not in the chart",
                    )
                )
                continue
            lines = _lines_for(ctx, report["id"])
            person_cfg = _person_named(ctx, report["person"])
            role = getattr(person_cfg, "role", "owner") if person_cfg else "owner"
            if role == "vendor":
                # Vendor-role booking (issue #120): every line is a project
                # cost on the project's own chart account — no overhead
                # accounts, the per-category table is never consulted.
                projects = sorted({str(line["project"] or "") for line in lines})
                account_ids, missing = _project_accounts(
                    ctx.tenant.expenses.project_account_template, projects, account_map
                )
                if missing:
                    parked += 1
                    approvals.append(
                        ApprovalSpec(
                            key=f"expmap:proj:{report['id']}",
                            action_type="expenses.qbo_map_project_account",
                            params={
                                "report_id": str(report["id"]),
                                "projects": ", ".join(missing),
                                "template": ctx.tenant.expenses.project_account_template
                                or "(unset)",
                            },
                            reason="no chart account for these projects; create the "
                            "account(s) the template names, or set "
                            "[expenses].project_account_template — never a guess",
                        )
                    )
                    continue
                group_by = "project"
            else:
                group_by = "category"
                categories = sorted({line["category"] for line in lines})
                mapping = ctx.tenant.expenses.category_accounts
                account_ids = {}
                missing = []
                for category in categories:
                    fqn = mapping.get(category, "")
                    acct = account_map.get(fqn) if fqn else None
                    if acct is None:
                        missing.append(category)
                    else:
                        account_ids[category] = acct
                if missing:
                    parked += 1
                    approvals.append(
                        ApprovalSpec(
                            key=f"expmap:acct:{report['id']}",
                            action_type="expenses.qbo_map_category",
                            params={
                                "report_id": str(report["id"]),
                                "categories": ", ".join(missing),
                            },
                            reason="no chart account mapped for these categories; set "
                            "[expenses].category_accounts — never a guess",
                        )
                    )
                    continue
            # W1 rule 2: an existing same-payee same-amount transaction in the
            # window is exactly the owner having hand-coded it already.
            lookalike = None
            for txn in recent:
                if str(txn.get("qbo_id")) in own_ids:
                    continue  # rule 3: never read our own writing as evidence
                if str(txn.get("vendor", "")).strip().lower() != payee_name.strip().lower():
                    continue
                if int(txn.get("amount_cents", -1)) != int(report["total_cents"]):
                    continue
                lookalike = txn
                break
            if lookalike is not None:
                # Record-only (03-F7): nothing consumes this card's resolution,
                # so approving or rejecting it executes nothing and the report
                # stays Open; the stable key is right for a card that only
                # records. It blocks the record rather than parking an ask.
                blocked += 1
                existing = str(lookalike.get("qbo_id"))
                approvals.append(
                    ApprovalSpec(
                        key=f"expdupq:{report['id']}",
                        action_type="expenses.qbo_duplicate_review",
                        params={"report_id": str(report["id"]), "existing": existing},
                        reason=f"a similar record already exists in the accounting system "
                        f"({existing}: same payee, same amount); the engine never writes "
                        "over it. This card is record-only: approving or rejecting it "
                        "executes nothing and the report stays Open until a human "
                        "reconciles the existing record by hand",
                    )
                )
                continue
            payload = _purchase_payload(
                report,
                lines,
                payee_id=payee_id,
                bank_account_id=bank_id,
                account_ids=account_ids,
                txn_date=txn_date,
                group_by=group_by,
            )
            try:
                created = client.create_purchase(payload)
            except QboApiError as exc:
                blocked += 1
                anomalies.append(
                    Anomaly(
                        code="expenses.qbo_purchase_rejected",
                        detail=f"report #{report['id']}: {exc}",
                    )
                )
                continue
            # #314: .get(key, default) falls back only when the key is
            # ABSENT; a QBO create response can carry an explicit null Id,
            # which str(None) turns into the truthy string "None" and would
            # defeat the `if not purchase_id` guard just below.
            purchase_id = str(created.get("Id") or "")
            if not purchase_id:
                anomalies.append(
                    Anomaly(
                        code="expenses.qbo_create_unconfirmed",
                        detail=f"report #{report['id']}: create returned no Id; stopped",
                    )
                )
                break
            # Honesty audit 2026-09-03 (03-F1, S1): the Purchase exists the
            # instant create returns. Remember it BEFORE the readback (status
            # stays Open: unverified) so a readback that raises or disagrees
            # can never leave a Purchase the ledger has forgotten, would
            # re-create, or would mistake for the owner's hand-coding.
            _update_report(ctx, report["id"], qbo_purchase_id=purchase_id)
            verified, note = _readback_purchase(client, purchase_id, report["total_cents"])
            events.append(
                EventSpec(
                    key=f"exppurchase:{report['idempotency_key']}",
                    event_type="expense.qbo_purchase_created",
                    payload={
                        "report_id": report["id"],
                        "person": report["person"],
                        "total_cents": report["total_cents"],
                        "qbo_purchase_id": purchase_id,
                        "verified": verified,
                        "readback": note,
                    },
                )
            )
            if not verified:
                unverified += 1
                anomalies.append(_readback_anomaly(report["id"], purchase_id, note))
                actions.append(
                    f"recorded report #{report['id']} -> Purchase {purchase_id} "
                    f"(UNVERIFIED: {note})"
                )
                break
            _complete_record(ctx, report, card, purchase_id)
            recorded += 1
            actions.append(f"recorded report #{report['id']} -> Purchase {purchase_id}")

    # -- leg 2: safety net — evidence matching an unconfirmed Open report -----
    unconfirmed = [r for r in open_reports if r["id"] not in confirmed]
    if unconfirmed and not ctx.shadow:
        try:
            client = client or _qbo_write_client(ctx)
            evidence = client.fetch_evidence(since=(datetime.now(UTC) - window).date().isoformat())
        except Exception as exc:
            # The net is best-effort; recording never depends on it. But a
            # net that is down must say so (03-F13), or a week of failures
            # reads exactly like a week with no evidence.
            evidence = []
            anomalies.append(
                Anomaly(
                    code="expenses.safety_net_unavailable",
                    detail=f"clearing evidence could not be fetched this run ({exc}); "
                    "no open report was scanned for a matching payment",
                )
            )
        own_ids = _own_purchase_ids(ctx)
        # A vendor-role person's payment clears under the VENDOR record's
        # name, so evidence matches through both names back to the person.
        payee_to_person = {}
        for p in _persons(ctx):
            payee_to_person[p.name.strip().lower()] = p.name
            qbo_vendor = getattr(p, "qbo_vendor", "")
            if qbo_vendor:
                payee_to_person[qbo_vendor.strip().lower()] = p.name
        for ev in evidence:
            payee = str(getattr(ev, "payee", "") or "").strip().lower()
            person_name = payee_to_person.get(payee)
            if person_name is None or str(getattr(ev, "qbo_id", "")) in own_ids:
                continue
            cents = int(getattr(ev, "amount_cents", 0))
            hits = [
                r
                for r in unconfirmed
                if int(r["total_cents"]) == abs(cents) and r["person"] == person_name
            ]
            if len(hits) > 1:
                approvals.append(
                    ApprovalSpec(
                        key=f"expamb:{abs(cents)}:{payee}",
                        action_type="expenses.match_ambiguous",
                        params={
                            "amount_cents": str(abs(cents)),
                            "person": person_name,
                            "report_ids": ",".join(str(r["id"]) for r in hits),
                        },
                        reason="one payment matches two open reports; a human picks, "
                        "never an auto-match",
                    )
                )
            elif len(hits) == 1:
                approvals.append(
                    _confirm_spec(
                        ctx,
                        hits[0],
                        reason="cleared-payment evidence matches this open report; "
                        "approve to let the engine record the split",
                        evidence=str(getattr(ev, "qbo_id", "")),
                    )
                )

    # -- leg 3: CSV clearing (W2 Option B) ------------------------------------
    cleared = 0
    csv_param = str(ctx.params.get("bank_csv") or "")
    if csv_param and not ctx.shadow:
        from ...adapters.bank_csv import parse_bank_csv

        lines = parse_bank_csv(csv_param, ctx.tenant.bank_csv)
        consumed: set[int] = set()
        # Lines consumed by PRIOR runs stay consumed: each past clearing
        # event recorded its line's (date, amount), so re-presenting a CSV
        # containing that line must not let it clear a second same-amount
        # report (issue #133).
        for ev in _events_of(ctx, "expense.reimbursement_cleared"):
            payload = ev.get("payload", {})
            date = str(payload.get("cleared_date") or "")
            cents = int(payload.get("amount_cents") or 0)
            for i, line in enumerate(lines):
                if i in consumed:
                    continue
                if line.date == date and int(abs(line.amount) * 100) == cents:
                    consumed.add(i)
                    break
        for report in _reports(ctx, STATUS_RECORDED):
            total = int(report["total_cents"])
            # Issue #133: payment PRECEDES approval by design (the confirm
            # card's reason says "approve once the reimbursement has been
            # paid" — live shape: check 3050 cleared 8/11, card approved
            # 8/12), so reimbursed_date — the approval day — must never
            # bound the window. The report month is the true floor: a
            # reimbursement cannot clear before the month it reimburses.
            month = str(report["month"] or "")
            start = f"{month}-01" if month else ""
            # The instrument recorded at approval (issue #115: check 3050)
            # disqualifies same-amount lines carrying a DIFFERENT number;
            # a line with no number still clears on amount+date (ACH/Zelle
            # exports omit the check column).
            ref_digits = "".join(re.findall(r"\d+", str(report.get("instrument_ref") or "")))
            for i, line in enumerate(lines):
                if i in consumed:
                    continue
                line_cents = int(abs(line.amount) * 100)
                if line_cents != total:
                    continue
                if start and line.date < start:
                    continue
                line_digits = "".join(re.findall(r"\d+", str(line.check_ref or "")))
                if ref_digits and line_digits and ref_digits != line_digits:
                    continue
                # A line clears exactly one report (issue #133): without
                # consumption, one bank line "explains" every same-amount
                # recorded report.
                consumed.add(i)
                _set_report_status(ctx, report["id"], STATUS_REIMBURSED, cleared_date=line.date)
                cleared += 1
                actions.append(f"report #{report['id']} cleared {line.date}")
                events.append(
                    EventSpec(
                        key=f"expcleared:{report['idempotency_key']}",
                        event_type="expense.reimbursement_cleared",
                        payload={
                            "report_id": report["id"],
                            "cleared_date": line.date,
                            "amount_cents": total,
                        },
                    )
                )
                break

    return JobOutput(
        status="ok",
        summary="expenses match: "
        + (f"unverified {unverified}, " if unverified else "")
        + f"recorded {recorded}, parked {parked}, blocked {blocked}, cleared {cleared}",
        actions=actions,
        events=events,
        approvals=approvals,
        anomalies=anomalies,
    )
