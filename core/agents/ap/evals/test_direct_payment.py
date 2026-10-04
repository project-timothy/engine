"""ap: the hand-check lane — a cleared payment no ledger row explains.

Phase 7 row 7.4 (#213), specified in #257. Five shapes are the contract, each
drawn from real money in a live book (the tenant-specific names and amounts
live in the issues; invariant 5 keeps them out of core):

| shape                                                     | expected |
|-----------------------------------------------------------|----------|
| unregistered contractor, no ledger row, contractor coding  | card     |
| registered contractor, hand checks that never hit the book | card     |
| payee-less payment with contractor coding                  | card     |
| freight vendor, same GL account as contract labor          | NO card  |
| owner loan payoff                                          | NO card  |

The freight row is the one that matters most. Freight is excluded from
1099-NEC reporting, and in this chart style it codes to the SAME GL account
as subcontract labor. An implementation that reads the account and not the
registry's cost type cards it, the owner learns the lane cries wolf, and the
escapes it exists to stop resume.

Why the lane exists: a 1099 needs four things kept by hand in four places,
and both existing detectors start downstream of a ledger row (the vendor-1099
lens starts FROM the registry; the CPA appendix builds taxpayer units from
``ap_invoices``). A contractor paid direct from checking is invisible to
both, so he is silently absent from the January packet.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.adapters.qbo import QboEvidence, normalize_purchase
from core.agents.ap import direct_payment as dp
from core.agents.ap import store
from core.agents.ap.registry import VendorEntry, VendorRegistry
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

# The house spelling under test: a project subaccount that both contract
# labor and freight code to, which is exactly why the account cannot be the
# discriminator.
PROJECT_ACCOUNT = "Cost of Goods Sold:Project Expense - PN00_0101"

UNREGISTERED = "Dana Whitfield"  # contractor with no registry row
REGISTERED = "Ray Ostlund"  # contractor with a row and a W-9
FREIGHT = "Cartway Freight"  # the true negative


def _registry(**entries: VendorEntry) -> VendorRegistry:
    return VendorRegistry(entries=dict(entries))


def _ev(**kw) -> QboEvidence:
    base = {
        "qbo_id": "Purchase:1",
        "txn_type": "Purchase",
        "payee": "",
        "amount_cents": 260000,
        "date": "2026-03-26",
        "check_ref": "",
        "accounts": [PROJECT_ACCOUNT],
    }
    base.update(kw)
    return QboEvidence(**base)


# --------------------------------------------------------------------------
# The five regression shapes, against the pure proposer.
# --------------------------------------------------------------------------


def test_unregistered_contractor_cards():
    """Shape 1: no registry row, no ledger row, contractor-shaped coding.

    The registry cannot classify him, so the coding is the only signal left.
    This is the shape that stayed invisible for nine months.
    """
    proposal = dp.propose(
        _ev(qbo_id="Purchase:83", payee=UNREGISTERED, amount_cents=260000),
        "out_of_scope",
        registry=_registry(),
    )
    assert proposal is not None
    assert proposal.payee == UNREGISTERED
    assert proposal.amount_cents == 260000
    assert proposal.project_hint == "P00_0101"
    assert "unregistered payee" in proposal.basis


def test_registered_contractor_cards_on_cost_type():
    """Shape 2: registered, W-9 on file, paid by hand checks the journal
    never saw. He HAS ledger presence, so he lands in ``unknown``."""
    reg = _registry(ostlund=VendorEntry(vendor=REGISTERED, cost_type="Subcontractors"))
    proposal = dp.propose(
        _ev(qbo_id="Purchase:91", payee=REGISTERED, amount_cents=243125),
        "unknown",
        registry=reg,
    )
    assert proposal is not None
    assert proposal.cost_type == "Subcontractors"
    assert "cost type" in proposal.basis


def test_payee_less_payment_cards_and_demands_a_payee():
    """Shape 3: the bank feed named nobody. The card proposes; the owner
    names. A payment that counts toward nobody appears on nobody's 1099."""
    proposal = dp.propose(
        _ev(qbo_id="Purchase:64", payee="", amount_cents=243125),
        "out_of_scope",
        registry=_registry(),
    )
    assert proposal is not None
    assert proposal.needs_payee is True
    assert "no payee at all" in proposal.basis


def test_freight_never_cards():
    """Shape 4, THE true negative. Freight is excluded from 1099-NEC, and it
    codes to the same account as subcontract labor, so only the registry's
    cost type separates them. An account-reading implementation fails here."""
    reg = _registry(cartway=VendorEntry(vendor=FREIGHT, cost_type="Freight/Shipping"))
    assert (
        dp.propose(
            _ev(qbo_id="Purchase:120", payee=FREIGHT, amount_cents=191500),
            "out_of_scope",
            registry=reg,
        )
        is None
    )


def test_owner_loan_payoff_never_cards():
    """Shape 5: a large payee-less clearing that is not contractor work.
    Size is not the test; the coding is."""
    assert (
        dp.propose(
            _ev(
                qbo_id="Purchase:223",
                payee="",
                amount_cents=1375000,
                accounts=["Short Term Loans Payable"],
            ),
            "out_of_scope",
            registry=_registry(),
        )
        is None
    )


# --------------------------------------------------------------------------
# Boundaries.
# --------------------------------------------------------------------------


def test_explained_decisions_never_card():
    """settle / already_recorded / review mean the book has an answer."""
    for kind in ("settle", "already_recorded", "review"):
        assert dp.propose(_ev(payee=UNREGISTERED), kind, registry=_registry()) is None


def test_floor_throttles_the_first_pass():
    """The floor is a volume throttle, not a tax rule: switching the lane on
    must not dump every small payee-less Meals charge into the queue."""
    small = _ev(qbo_id="Purchase:9", amount_cents=4000)
    assert dp.propose(small, "out_of_scope", registry=_registry()) is None
    assert dp.propose(small, "out_of_scope", registry=_registry(), floor_cents=0) is not None


def test_ledger_alias_resolves_to_the_registry_row():
    """A vendor whose registry name and everyday spelling differ must resolve
    to one identity. Matching names locally instead of through the registry
    is what let a whole payee's clearings sit unnoticed in the sink."""
    reg = _registry(
        vance=VendorEntry(
            vendor="Robert Vance",
            cost_type="Freight/Shipping",
            ledger_aliases=["Bob Vance"],
        )
    )
    # The alias resolves, the non-1099 cost type is found, so no card.
    assert (
        dp.propose(_ev(payee="Bob Vance", amount_cents=2737611), "out_of_scope", registry=reg)
        is None
    )


def test_split_purchase_reads_every_account():
    """One contractor line inside a mixed payment still means a person was
    paid for work."""
    proposal = dp.propose(
        _ev(accounts=["Office Supplies", PROJECT_ACCOUNT]),
        "out_of_scope",
        registry=_registry(),
    )
    assert proposal is not None
    assert proposal.project_hint == "P00_0101"


def test_normalize_purchase_carries_the_account_names():
    """The coding has to survive normalization or the lane is blind."""
    ev = normalize_purchase(
        {
            "Id": "83",
            "TotalAmt": "2600.00",
            "TxnDate": "2026-03-26",
            "EntityRef": {"name": UNREGISTERED},
            "Line": [
                {"AccountBasedExpenseLineDetail": {"AccountRef": {"name": PROJECT_ACCOUNT}}},
                {"AccountBasedExpenseLineDetail": {"AccountRef": {"name": PROJECT_ACCOUNT}}},
            ],
        }
    )
    assert ev.accounts == [PROJECT_ACCOUNT]  # de-duplicated
    assert ev.amount_cents == 260000


# --------------------------------------------------------------------------
# End to end: card parks, approval creates the row, re-runs stay idempotent.
# --------------------------------------------------------------------------


def _evidence_file(tmp_path: Path, *entries) -> Path:
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(list(entries)))
    return p


def _run(ledger_dir: Path, evidence: Path, **params):
    return run(
        "demo",
        "ap",
        "reconcile",
        params={"evidence_file": str(evidence), "direct_payment_cards": "true", **params},
        ledger_dir=ledger_dir,
    )


PAYMENT = {
    "qbo_id": "Purchase:83",
    "txn_type": "Purchase",
    "payee": UNREGISTERED,
    "amount_cents": 260000,
    "date": "2026-03-26",
    "check_ref": "",
    "accounts": [PROJECT_ACCOUNT],
}


def _cards(ledger_dir: Path, action_type: str) -> list[dict]:
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        rows = ledger.conn.execute(
            "SELECT id, status, params_json FROM approval_queue WHERE action_type = ?",
            (action_type,),
        ).fetchall()
    return [
        {"id": r["id"], "status": r["status"], "params": json.loads(r["params_json"])} for r in rows
    ]


def _approve(ledger_dir: Path, card_id: int, project: str = "P00_0101") -> None:
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        ledger.conn.execute(
            "UPDATE approval_queue SET status = 'approved', "
            "params_json = json_set(params_json, '$.project', ?) WHERE id = ?",
            (project, card_id),
        )
        ledger.conn.commit()


def test_card_parks_then_approval_creates_the_payable_row(tmp_path):
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path, PAYMENT)

    first = _run(d, ev)
    assert first.status in ("ok", "needs_approval")

    parked = _cards(d, "ap.record_direct_payment")
    assert len(parked) == 1, "the unexplained contractor payment should park one card"
    assert parked[0]["params"]["project_hint"] == "P00_0101"

    _approve(d, parked[0]["id"])
    _run(d, ev)

    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        rows = store.invoices_by_number(ledger, "demo", "DP-Purchase-83")
    assert len(rows) == 1
    row = rows[0]
    assert row["vendor"] == UNREGISTERED
    assert row["amount_cents"] == 260000
    assert row["status"] == "Paid"
    assert row["project"] == "P00_0101"
    assert row["payment_date"] == "2026-03-26"


def test_a_recorded_payment_never_cards_or_doubles_on_re_run(tmp_path):
    """The bug this design is most likely to have: the row we create carries
    the owner's payee while the evidence still carries whatever the feed
    said, so the matcher may not connect them and the card re-parks forever."""
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path, PAYMENT)
    _run(d, ev)
    _approve(d, _cards(d, "ap.record_direct_payment")[0]["id"])

    _run(d, ev)
    _run(d, ev, direct_payment_floor_cents="1")  # a fresh key, so it really re-runs

    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        rows = ledger.conn.execute(
            "SELECT id FROM ap_invoices WHERE invoice_number = 'DP-Purchase-83'"
        ).fetchall()
    assert len(rows) == 1, "a re-run must not create a second row"
    assert len(_cards(d, "ap.record_direct_payment")) == 1, "and must not re-card"


def test_rejected_card_is_an_answer_and_never_re_asks(tmp_path):
    """'This is not a payable' is a decision. Re-asking it nightly is the nag
    the lane exists to avoid."""
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path, PAYMENT)
    _run(d, ev)
    parked = _cards(d, "ap.record_direct_payment")
    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        ledger.conn.execute(
            "UPDATE approval_queue SET status = 'rejected' WHERE id = ?", (parked[0]["id"],)
        )
        ledger.conn.commit()

    _run(d, ev, direct_payment_floor_cents="1")

    assert len(_cards(d, "ap.record_direct_payment")) == 1, "a rejected card must not re-park"
    with Ledger.open(root) as ledger:
        rows = ledger.conn.execute(
            "SELECT id FROM ap_invoices WHERE invoice_number = 'DP-Purchase-83'"
        ).fetchall()
    assert rows == [], "a rejection creates nothing"


def test_lane_off_parks_nothing(tmp_path):
    """Off by default, and off means silent: a tenant whose contractors all
    invoice through AP sees no cards at all."""
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path, PAYMENT)
    run(
        "demo",
        "ap",
        "reconcile",
        params={"evidence_file": str(ev), "direct_payment_cards": "false"},
        ledger_dir=d,
    )
    assert _cards(d, "ap.record_direct_payment") == []


def test_approval_check_refuses_a_card_it_cannot_execute():
    """No payee and no project are both unguessable, so the queue refuses
    rather than inventing either (invariant 2)."""
    from core.agents.ap.jobs import check_record_direct_payment as check

    assert check(None, "demo", {"payee": "", "project_hint": "P00_0101"}) is not None
    assert check(None, "demo", {"payee": "X", "project_hint": "", "project": ""}) is not None
    assert check(None, "demo", {"payee": "X", "project_hint": "P00_0101"}) is None
    assert check(None, "demo", {"payee": "", "payee_override": "X", "project": "P00_0102"}) is None


def test_lane_is_off_by_default():
    from core.engine.config import QboSettings

    assert QboSettings().direct_payment_cards is False


# --------------------------------------------------------------------------
# #287: a check flagged unknown is UNANSWERED, not answered.
#
# The lane was flipped on the day after the statement tier's first live run
# had already flagged every unexplained check as unknown. Three places
# counted a flagged check as explained and skipped the lane with it, so the
# flip parked nothing for any check that had already cleared, and the owner
# act the lane exists for (flip it on, answer the hand checks, close the
# contractor's ledger gap) was inert. An unknown event is the unanswered
# state written down once so the owner is not asked twice; it is not an
# answer. A card must still park, once per physical check, from whichever
# source saw it.
# --------------------------------------------------------------------------

DP_CARD = "ap.record_direct_payment"
UNKNOWN_EVENT = "ap.reconcile.unknown"
# The demo tenant's deliberately different export header (Posted/Memo/Chk/Value).
STATEMENT_HEADER = "Posted,Memo,Chk,Value\n"
CHECK = "3029"
CHECK_CENTS = 243125
CHECK_DATE = "2026-03-05"

# The same physical check as the accounting feed reports it: a hand check to
# a contractor, no payee on the record, coded to project expense.
HAND_CHECK = {
    "qbo_id": "Purchase:64",
    "txn_type": "Purchase",
    "payee": "",
    "amount_cents": CHECK_CENTS,
    "date": CHECK_DATE,
    "check_ref": CHECK,
    "accounts": [PROJECT_ACCOUNT],
}


def _statement(tmp_path: Path, *lines, name: str = "statement.csv") -> Path:
    """lines: (posted, memo, chk, value) in the demo tenant's format."""
    p = tmp_path / name
    p.write_text(STATEMENT_HEADER + "".join(f"{d},{m},{c},{v}\n" for d, m, c, v in lines))
    return p


def _events(ledger_dir: Path, event_type: str) -> list[dict]:
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


def _seed_row(
    ledger_dir: Path,
    *,
    vendor: str,
    number: str,
    cents: int,
    check_ref: str,
    payment_id: str = "",
    status: str = "Scheduled",
    payment_date: str = CHECK_DATE,
) -> None:
    """One row the owner recorded a check number on, Scheduled or settled."""
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        inv_id, _ = store.insert_invoice(
            ledger,
            tenant="demo",
            vendor=vendor,
            invoice_number=number,
            amount_cents=cents,
            status=status,
            invoice_date="2026-03-01",
        )
        store.record_payment_details(
            ledger, invoice_id=inv_id, payment_date=payment_date, check_ref=check_ref
        )
        if payment_id:
            store.record_qbo_ids(ledger, invoice_id=inv_id, payment_id=payment_id)


def test_a_statement_check_already_flagged_unknown_still_cards_on_the_flip(tmp_path):
    """The reported bug, from the statement side. The tier flags the check
    the night before the owner flips the lane on; the flip must still reach
    it, and must not write the unknown a second time."""
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path)
    csv = _statement(tmp_path, (CHECK_DATE, f"CHECK {CHECK}", CHECK, "-2431.25"))

    off = _run(d, ev, bank_csv=str(csv), direct_payment_cards="false")
    assert [a.code for a in off.anomalies] == ["ap.reconcile.unknown_payment"]
    assert len(_events(d, UNKNOWN_EVENT)) == 1
    assert _cards(d, DP_CARD) == []

    on = _run(d, ev, bank_csv=str(csv))

    parked = _cards(d, DP_CARD)
    assert len(parked) == 1, "the flip must reach a check the tier already flagged"
    assert parked[0]["params"]["check_ref"] == CHECK
    assert parked[0]["params"]["amount_cents"] == CHECK_CENTS
    assert parked[0]["params"]["source"] == "statement"
    # Everything else is byte-identical to the old behaviour: the line still
    # counts as already recorded, the unknown stays once-only, no anomaly.
    assert len(_events(d, UNKNOWN_EVENT)) == 1
    assert on.anomalies == []
    assert "unknown 0" in on.summary
    assert "already recorded: 1" in on.summary

    again = _run(d, ev, bank_csv=str(csv), direct_payment_floor_cents="1")
    assert len(_cards(d, DP_CARD)) == 1, "a second run must not re-card"
    assert len(_events(d, UNKNOWN_EVENT)) == 1
    assert again.anomalies == []


def test_a_feed_payment_the_statement_already_flagged_still_cards(tmp_path):
    """The deferred-unknowns pass. The bank saw the check first; the same
    physical check arrives in the accounting feed after the flip."""
    d = tmp_path / "d"
    csv = _statement(tmp_path, (CHECK_DATE, f"CHECK {CHECK}", CHECK, "-2431.25"))

    off = _run(d, _evidence_file(tmp_path), bank_csv=str(csv), direct_payment_cards="false")
    assert [a.code for a in off.anomalies] == ["ap.reconcile.unknown_payment"]
    (flagged,) = _events(d, UNKNOWN_EVENT)
    assert flagged["payload"]["source"] == "statement"

    on = _run(d, _evidence_file(tmp_path, HAND_CHECK))

    parked = _cards(d, DP_CARD)
    assert len(parked) == 1, "the feed must still card a check the statement flagged"
    assert parked[0]["params"]["qbo_id"] == "Purchase:64"
    assert parked[0]["params"]["source"] == "qbo"
    assert len(_events(d, UNKNOWN_EVENT)) == 1, "the check was already asked about once"
    assert on.anomalies == []
    assert "unknown 0" in on.summary


def test_a_statement_line_the_feed_already_flagged_still_cards(tmp_path):
    """The mirror image: the feed flagged the check first, and the flip must
    still put that check in front of the owner exactly once.

    Retargeted by #293, intent intact. Under #292 the feed's flagged-only id
    was still filtered out entirely, so the statement line was the only path
    left and the card carried ``source = statement``. The fourth skip point
    is closed now, so the feed's own evidence reaches the lane first and
    parks the card, and the statement line for the same physical check
    dedupes against it. The check that matters is unchanged: one card, this
    check, this amount, no second unknown.
    """
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path, HAND_CHECK)

    off = _run(d, ev, direct_payment_cards="false")
    assert [a.code for a in off.anomalies] == ["ap.reconcile.unknown_payment"]
    (flagged,) = _events(d, UNKNOWN_EVENT)
    assert flagged["payload"]["check_ref"] == CHECK
    assert "statement_id" not in flagged["payload"]

    csv = _statement(tmp_path, (CHECK_DATE, f"CHECK {CHECK}", CHECK, "-2431.25"))
    on = _run(d, ev, bank_csv=str(csv))

    parked = _cards(d, DP_CARD)
    assert len(parked) == 1, "the flip must card a check the feed flagged, once"
    assert parked[0]["params"]["check_ref"] == CHECK
    assert parked[0]["params"]["amount_cents"] == CHECK_CENTS
    # The richer source wins the race by construction: the feed record
    # carries the payee and the coding, and a statement line carries
    # neither, so its card has to ask the owner for both.
    assert parked[0]["params"]["source"] == "qbo"
    assert parked[0]["params"]["qbo_id"] == HAND_CHECK["qbo_id"]
    assert len(_events(d, UNKNOWN_EVENT)) == 1
    assert on.anomalies == []
    assert "unknown 0" in on.summary


def test_one_card_per_physical_check_when_both_sources_flag_it(tmp_path):
    """Both sources see the same check, and the flip comes after. The owner
    answers a check once, so exactly one card parks whichever source reaches
    the lane first, and the flip itself writes no new unknown."""
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path, HAND_CHECK)
    csv = _statement(tmp_path, (CHECK_DATE, f"CHECK {CHECK}", CHECK, "-2431.25"))

    _run(d, ev, bank_csv=str(csv), direct_payment_cards="false")
    before = len(_events(d, UNKNOWN_EVENT))
    on = _run(d, ev, bank_csv=str(csv))

    assert len(_cards(d, DP_CARD)) == 1
    assert len(_events(d, UNKNOWN_EVENT)) == before
    assert on.anomalies == []


def test_a_settled_statement_line_never_cards(tmp_path):
    """The other half of the split. A line that settled a row is a real
    answer, and the lane must stay silent about it at any floor."""
    d = tmp_path / "d"
    _seed_row(
        d,
        vendor="Acme Tooling",
        number="N-1",
        cents=CHECK_CENTS,
        check_ref=CHECK,
        payment_id="BillPayment:BP1",
    )
    ev = _evidence_file(tmp_path)
    csv = _statement(tmp_path, (CHECK_DATE, f"CHECK {CHECK}", CHECK, "-2431.25"))

    off = _run(d, ev, bank_csv=str(csv), direct_payment_cards="false")
    assert off.status == "ok", off.summary
    (paid,) = _events(d, "ap.reconcile.paid")
    assert paid["payload"]["statement_id"].startswith("stmt:")

    on = _run(d, ev, bank_csv=str(csv), direct_payment_floor_cents="1")

    assert _cards(d, DP_CARD) == [], "a settled line is answered, not unknown"
    assert _events(d, UNKNOWN_EVENT) == []
    assert on.anomalies == []


def test_a_decided_review_card_answers_the_line_and_it_never_cards(tmp_path):
    """A review card the owner decided is an answer too: the reused check
    number went to a human, and the hand-check lane must not ask again."""
    d = tmp_path / "d"
    for vendor, number in (("Acme Tooling", "A-1"), ("Beta Supply", "B-1")):
        _seed_row(d, vendor=vendor, number=number, cents=CHECK_CENTS, check_ref=CHECK)
    ev = _evidence_file(tmp_path)
    csv = _statement(tmp_path, (CHECK_DATE, f"CHECK {CHECK}", CHECK, "-2431.25"))

    _run(d, ev, bank_csv=str(csv), direct_payment_cards="false")
    review = _cards(d, "ap.reconcile_review")
    assert len(review) == 1
    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        ledger.conn.execute(
            "UPDATE approval_queue SET status = 'rejected' WHERE id = ?", (review[0]["id"],)
        )
        ledger.conn.commit()

    on = _run(d, ev, bank_csv=str(csv), direct_payment_floor_cents="1")

    assert _cards(d, DP_CARD) == [], "the owner already answered this line"
    assert _events(d, UNKNOWN_EVENT) == []
    assert on.anomalies == []


# --------------------------------------------------------------------------
# #287 follow-up (PR #292): re-decide a flagged-only line before the lane.
#
# An unknown event is a snapshot of what the rules could see the night it
# was written, and the rules move. The legacy-reference tier landed after
# some lines had already been flagged by an earlier, thinner one, so those
# lines carry a question the book can now answer. Sending them straight to
# the hand-check lane carded two checks a settled row explains perfectly
# well: one whose reference spells the number with a prefix, one naming a
# two-check split. The decision is re-run and only its cardable kinds reach
# the lane; the line itself stays exactly as once-only as it was, with no
# backfill, no settle, no review card, and no new event.
# --------------------------------------------------------------------------

REF_BACKFILLED = "ap.reconcile.ref_backfilled"
PAID_EVENT = "ap.reconcile.paid"


def test_a_flagged_only_line_a_settled_reference_explains_never_cards(tmp_path):
    """The prefix shape: the row's reference is the number with letters in
    front of it, and the row is settled for the cleared amount."""
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path)
    csv = _statement(tmp_path, ("2026-04-24", "CHECK 3039", "3039", "-3891.44"))

    off = _run(d, ev, bank_csv=str(csv), direct_payment_cards="false")
    assert [a.code for a in off.anomalies] == ["ap.reconcile.unknown_payment"]
    assert len(_events(d, UNKNOWN_EVENT)) == 1

    # The row was there the whole time, spelling the number its own way.
    _seed_row(
        d,
        vendor="Acme Tooling",
        number="1647",
        cents=389144,
        check_ref="CK3039",
        status="Paid",
        payment_date="2026-05-08",
    )

    on = _run(d, ev, bank_csv=str(csv))

    assert _cards(d, DP_CARD) == [], "the book already names this check"
    assert len(_events(d, UNKNOWN_EVENT)) == 1, "the line stays once-only"
    assert _events(d, REF_BACKFILLED) == [], "a re-decide takes no side effect"
    assert _events(d, PAID_EVENT) == []
    assert _cards(d, "ap.reconcile_review") == []
    assert on.anomalies == []
    assert "already recorded: 1" in on.summary
    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        row = store.invoices_by_number(ledger, "demo", "1647")[0]
    assert row["check_ref"] == "CK3039", "no write touched the row"


def test_a_flagged_only_line_a_settled_split_reference_names_never_cards(tmp_path):
    """The split shape: one row paid with two checks names both, so the row's
    own amount answers neither line by itself and only the token rule can
    explain the clearing."""
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path)
    csv = _statement(tmp_path, ("2026-04-22", "CHECK 7028", "7028", "-10000.00"))

    off = _run(d, ev, bank_csv=str(csv), direct_payment_cards="false")
    assert [a.code for a in off.anomalies] == ["ap.reconcile.unknown_payment"]
    assert len(_events(d, UNKNOWN_EVENT)) == 1

    _seed_row(
        d,
        vendor="Beta Supply",
        number="20790",
        cents=1002800,
        check_ref="Check 7028+7029 (Apr 20)",
        status="Paid",
        payment_date="2026-04-20",
    )

    on = _run(d, ev, bank_csv=str(csv))

    assert _cards(d, DP_CARD) == [], "a legacy reference naming the check is an answer"
    assert len(_events(d, UNKNOWN_EVENT)) == 1
    assert _events(d, REF_BACKFILLED) == []
    assert _events(d, PAID_EVENT) == []
    assert _cards(d, "ap.reconcile_review") == []
    assert on.anomalies == []


def test_a_flagged_only_line_no_row_explains_still_cards_exactly_once(tmp_path):
    """The re-decide must not cost the lane the checks it exists for: with no
    row under the number at all, the decision is still unknown."""
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path)
    csv = _statement(tmp_path, (CHECK_DATE, f"CHECK {CHECK}", CHECK, "-2431.25"))

    _run(d, ev, bank_csv=str(csv), direct_payment_cards="false")
    # A settled row under a DIFFERENT number is not an answer to this check.
    _seed_row(
        d,
        vendor="Beta Supply",
        number="B-9",
        cents=CHECK_CENTS,
        check_ref="CK3040",
        status="Paid",
    )

    on = _run(d, ev, bank_csv=str(csv))

    parked = _cards(d, DP_CARD)
    assert len(parked) == 1
    assert parked[0]["params"]["check_ref"] == CHECK
    assert len(_events(d, UNKNOWN_EVENT)) == 1
    assert on.anomalies == []

    again = _run(d, ev, bank_csv=str(csv), direct_payment_floor_cents="1")
    assert len(_cards(d, DP_CARD)) == 1, "and still only once"
    assert again.anomalies == []


# --------------------------------------------------------------------------
# #293: the FOURTH skip point, on the accounting feed's own side.
#
# ``_reconcile_explained()`` filters every id that ever carried an unknown
# event out of the evidence list before the decision loop runs, so a payment
# flagged the night before the flip never reaches the lane at all. A CHECK
# survives that skip, because the bank statement sees the same physical
# check and the statement side was repaired in #292. A payee-less NON-check
# payment (an ACH, a debit card, a bill pay with no number) has no second
# source: nothing else in the engine ever sees it again, so it stays
# invisible to the hand-check lane forever.
#
# The repair is the same shape as the statement side: split the helper into
# answered and flagged-only, re-decide the flagged-only evidence with no
# side effect at all, and hand the lane the decision's own kind. The
# evidence stays OUT of the main loop, so no counter but ``already
# recorded`` moves, nothing settles, nothing parks review, and the once-only
# unknown is never written twice.
# --------------------------------------------------------------------------

OWNER = "Jo Prentice"  # an owner payee, curated onto the ignore list

# The live shape: the accounting feed named nobody, there is no check number
# to give the bank statement a second look at it, and the coding is
# contractor work.
FEED_ACH = {
    "qbo_id": "Purchase:304",
    "txn_type": "Purchase",
    "payee": "",
    "amount_cents": CHECK_CENTS,
    "date": CHECK_DATE,
    "check_ref": "",
    "accounts": [PROJECT_ACCOUNT],
}


def _seed_unknown_event(ledger_dir: Path, payment: dict) -> None:
    """The night before the flip: this payment was flagged unknown once, and
    that single event is the only thing that ever happened to it."""
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        run_row = ledger.conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        ledger.append_event(
            idempotency_key=f"seed:unknown:{payment['qbo_id']}",
            run_id=int(run_row["id"]),
            tenant="demo",
            agent="ap",
            event_type=UNKNOWN_EVENT,
            payload={
                "qbo_id": payment["qbo_id"],
                "payee": payment["payee"],
                "amount_cents": payment["amount_cents"],
                "date": payment["date"],
                "check_ref": payment["check_ref"],
            },
        )


def _seed_expense_report(ledger_dir: Path, *, report_id: int, cents: int, purchase_id: str) -> None:
    """One expense report the engine already recorded in the accounting
    system, exactly as ``expenses match`` leaves it."""
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        ledger.conn.execute(
            "INSERT INTO expense_report (id, idempotency_key, tenant, person, month, "
            "total_cents, status, created_at, updated_at, qbo_purchase_id) "
            "VALUES (?, ?, 'demo', 'Sam Vendor', '2026-03', ?, 'Reimbursed-Recorded', "
            "'2026-03-05T00:00:00Z', '2026-03-05T00:00:00Z', ?)",
            (report_id, f"exp-{report_id}", cents, purchase_id),
        )
        ledger.conn.commit()


def test_a_payee_less_feed_payment_flagged_unknown_still_cards_on_the_flip(tmp_path):
    """The reported bug. No check number means no second source, so this
    skip is permanent: the flip must reach the payment itself."""
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path, FEED_ACH)

    _run(d, ev, direct_payment_cards="false")
    assert _cards(d, DP_CARD) == [], "the lane was off the night it was flagged"
    _seed_unknown_event(d, FEED_ACH)

    on = _run(d, ev)

    parked = _cards(d, DP_CARD)
    assert len(parked) == 1, "the flip must reach a payment the feed already flagged"
    assert parked[0]["params"]["qbo_id"] == FEED_ACH["qbo_id"]
    assert parked[0]["params"]["amount_cents"] == CHECK_CENTS
    assert parked[0]["params"]["source"] == "qbo"
    assert parked[0]["params"]["decision"] == "out_of_scope", "the decision's own kind"
    # Everything else the skip did, it still does: the question stays asked
    # once, no anomaly, no review card, nothing settles.
    assert len(_events(d, UNKNOWN_EVENT)) == 1
    assert _cards(d, "ap.reconcile_review") == []
    assert _events(d, PAID_EVENT) == []
    assert on.anomalies == []
    assert "unknown 0" in on.summary
    assert "already recorded: 1" in on.summary

    again = _run(d, ev, direct_payment_floor_cents="1")
    assert len(_cards(d, DP_CARD)) == 1, "a second run must not re-card"
    assert len(_events(d, UNKNOWN_EVENT)) == 1
    assert again.anomalies == []


def test_a_flagged_only_feed_payment_an_expense_report_explains_never_cards(tmp_path):
    """The live shape #281 closed: the engine's own expense-report Purchase
    was flagged unknown every morning before the report join existed. The
    report answers it by exact id now, so the lane stays silent."""
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path, FEED_ACH)

    _run(d, ev, direct_payment_cards="false")
    _seed_unknown_event(d, FEED_ACH)
    _seed_expense_report(d, report_id=2, cents=CHECK_CENTS, purchase_id="304")

    on = _run(d, ev)

    assert _cards(d, DP_CARD) == [], "an expense report is not an unrecorded payable"
    assert len(_events(d, UNKNOWN_EVENT)) == 1
    assert on.anomalies == []
    assert "already recorded: 1" in on.summary


def test_a_flagged_only_feed_payment_a_settled_row_explains_never_cards(tmp_path):
    """The owner recorded the payable after the payment was flagged. A
    settled row for this payee, amount and date is a real answer, so the
    re-decide is what keeps the card from parking."""
    d = tmp_path / "d"
    payment = dict(FEED_ACH, qbo_id="Purchase:91", payee=REGISTERED)
    ev = _evidence_file(tmp_path, payment)

    _run(d, ev, direct_payment_cards="false")
    _seed_unknown_event(d, payment)
    _seed_row(
        d,
        vendor=REGISTERED,
        number="R-1",
        cents=CHECK_CENTS,
        check_ref="",
        status="Paid",
        payment_date=CHECK_DATE,
    )

    on = _run(d, ev)

    assert _cards(d, DP_CARD) == [], "the book already records this money"
    assert len(_events(d, UNKNOWN_EVENT)) == 1
    assert _events(d, PAID_EVENT) == [], "a re-decide takes no side effect"
    assert _cards(d, "ap.reconcile_review") == []
    assert on.anomalies == []
    assert "already recorded: 1" in on.summary


def test_a_flagged_only_feed_payment_on_the_ignore_list_never_cards(tmp_path):
    """The curated ignore list is an owner decision about payees, and it
    holds here too: an owner's own clearing is never a contractor payable,
    whatever the coding says."""
    d = tmp_path / "d"
    payment = dict(FEED_ACH, qbo_id="Purchase:77", payee=OWNER)
    ev = _evidence_file(tmp_path, payment)

    _run(d, ev, direct_payment_cards="false", ignore_payees=OWNER)
    _seed_unknown_event(d, payment)

    on = _run(d, ev, ignore_payees=OWNER)

    assert _cards(d, DP_CARD) == [], "an owner payee never cards"
    assert len(_events(d, UNKNOWN_EVENT)) == 1
    assert on.anomalies == []
    assert "already recorded: 1" in on.summary


# --------------------------------------------------------------------------
# #305: one physical check, two sources, and only ONE of them has the number.
#
# The first live day of the lane. A hand check cleared the bank and the bank
# statement carried its number; the same money reached the accounting feed as
# a payment with no document number at all. ``_carded_checks()`` keys on
# (check number, amount), so the two sources could not recognise one physical
# check between them and the owner was asked twice about the same money.
# Approving both would have created two payable rows for one payment.
#
# The second identity, for exactly the case where one side has no number:
# same cents AND the same cleared date (exact, or within the reconcile date
# slack) AND the numbered side's number on no other feed evidence in the
# window. Amount alone is never the discriminator here, and this is not that
# rule: it is amount plus date plus the absence of any competing evidence,
# and it refuses to answer the moment two candidates fit.
# --------------------------------------------------------------------------

TWIN_CHECK = "4051"
TWIN_CENTS = 425000
TWIN_DATE = "2026-08-25"
TWIN_DOLLARS = "-4250.00"
SUPERSEDED_EVENT = "ap.direct_payment.superseded"

# The same physical check as the accounting feed reports it: a hand check to
# a contractor, coded to project expense, with no document number on the
# record. The bank statement line for it carries the number and nothing else.
TWIN_FEED = {
    "qbo_id": "Purchase:285",
    "txn_type": "Purchase",
    "payee": UNREGISTERED,
    "amount_cents": TWIN_CENTS,
    "date": TWIN_DATE,
    "check_ref": "",
    "accounts": [PROJECT_ACCOUNT],
}


def _twin_statement(tmp_path: Path, name: str = "twin.csv") -> Path:
    return _statement(
        tmp_path, (TWIN_DATE, f"CHECK {TWIN_CHECK}", TWIN_CHECK, TWIN_DOLLARS), name=name
    )


def test_cross_source_twin_reads_amount_date_and_competing_evidence():
    """The pure rule. Every clause is load-bearing and each one alone is not
    enough: amount on its own is the discriminator this lane refuses."""
    numbered = dp.Payment(
        ident="stmt:1", amount_cents=TWIN_CENTS, date=TWIN_DATE, check_ref=TWIN_CHECK
    )
    twin = dp.Payment(ident="Purchase:285", amount_cents=TWIN_CENTS, date=TWIN_DATE)

    assert dp.cross_source_twin(numbered, [twin]) is twin
    # Inside the slack window is still the same check: a bank posts the day
    # the accounting record does not.
    near = dp.Payment(ident="Purchase:285", amount_cents=TWIN_CENTS, date="2026-08-27")
    assert dp.cross_source_twin(numbered, [near]) is near
    # Beyond it, no.
    far = dp.Payment(ident="Purchase:999", amount_cents=TWIN_CENTS, date="2026-09-10")
    assert dp.cross_source_twin(numbered, [far]) is None
    # Two candidates fit: the lane does not guess which one cleared.
    other = dp.Payment(ident="Purchase:288", amount_cents=TWIN_CENTS, date="2026-08-27")
    assert dp.cross_source_twin(numbered, [twin, other]) is None
    # A cent apart is different money.
    cent = dp.Payment(ident="Purchase:290", amount_cents=TWIN_CENTS - 1, date=TWIN_DATE)
    assert dp.cross_source_twin(numbered, [cent]) is None
    # Competing evidence: the feed DOES know this number, on some other
    # record, so the statement's check belongs to that one.
    assert dp.cross_source_twin(numbered, [twin], feed_numbers=["Check 4051"]) is None
    # A candidate with a number of its own is not this rule's business: the
    # (number, amount) identity already answers that pair.
    numbered_too = dp.Payment(
        ident="Purchase:291", amount_cents=TWIN_CENTS, date=TWIN_DATE, check_ref="4052"
    )
    assert dp.cross_source_twin(numbered, [numbered_too]) is None
    # No number on the numbered side at all: there is nothing to cross.
    assert dp.cross_source_twin(dp.Payment("stmt:2", TWIN_CENTS, TWIN_DATE), [twin]) is None


def test_cross_source_pairs_refuse_a_twin_that_two_numbered_sides_claim():
    """Unique from BOTH sides or not at all. Two checks written the same day
    for the same amount cannot both be the one numberless payment."""
    a = dp.Payment("stmt:a", TWIN_CENTS, TWIN_DATE, TWIN_CHECK)
    b = dp.Payment("stmt:b", TWIN_CENTS, TWIN_DATE, "4052")
    twin = dp.Payment("Purchase:285", TWIN_CENTS, TWIN_DATE)

    assert dp.cross_source_pairs([a], [twin]) == {"stmt:a": "Purchase:285"}
    assert dp.cross_source_pairs([a, b], [twin]) == {}


def test_one_card_when_the_feed_side_of_a_statement_check_has_no_number(tmp_path):
    """The reported bug. Both sources see the check on the same day for the
    same cents and only the bank has the number, so exactly one card parks
    and it is the richer one: the feed record carries the payee and the
    coding a statement line can never have."""
    d = tmp_path / "d"
    ev = _evidence_file(tmp_path, TWIN_FEED)
    csv = _twin_statement(tmp_path)

    on = _run(d, ev, bank_csv=str(csv))

    parked = _cards(d, DP_CARD)
    assert len(parked) == 1, "one physical check is one card"
    assert parked[0]["params"]["qbo_id"] == TWIN_FEED["qbo_id"]
    assert parked[0]["params"]["source"] == "qbo"
    assert parked[0]["params"]["payee"] == UNREGISTERED
    assert on.status in ("ok", "needs_approval")

    again = _run(d, ev, bank_csv=str(csv), direct_payment_floor_cents="1")
    assert len(_cards(d, DP_CARD)) == 1, "and a second run must not add the other side"
    del again


def test_two_numberless_payments_at_the_same_cents_leave_every_card_parked(tmp_path):
    """No guess. A second feed payment for the same amount two days away puts
    two candidates inside the window, so the lane refuses to pair anything
    and every side asks its own question."""
    d = tmp_path / "d"
    second = dict(TWIN_FEED, qbo_id="Purchase:288", date="2026-08-27")
    ev = _evidence_file(tmp_path, TWIN_FEED, second)
    csv = _twin_statement(tmp_path)

    _run(d, ev, bank_csv=str(csv))

    parked = _cards(d, DP_CARD)
    idents = sorted(c["params"]["qbo_id"] for c in parked)
    assert len(parked) == 3, "two feed payments and the statement line all ask"
    assert idents[:2] == ["Purchase:285", "Purchase:288"]
    assert [c["params"]["source"] for c in parked].count("statement") == 1


def test_the_feed_card_names_the_statement_card_that_asked_first(tmp_path):
    """Today's live shape: the bank saw the check first and parked the thin
    card, then the accounting record arrived. The richer card still parks,
    and its reason says which card it answers."""
    d = tmp_path / "d"
    csv = _twin_statement(tmp_path)

    _run(d, _evidence_file(tmp_path), bank_csv=str(csv))
    first = _cards(d, DP_CARD)
    assert len(first) == 1 and first[0]["params"]["source"] == "statement"

    _run(d, _evidence_file(tmp_path, TWIN_FEED), bank_csv=str(csv))

    parked = _cards(d, DP_CARD)
    assert len(parked) == 2, "the richer side still asks"
    feed = [c for c in parked if c["params"]["source"] == "qbo"]
    assert len(feed) == 1
    assert feed[0]["params"]["supersedes_card"] == str(first[0]["id"])


def test_an_approved_feed_card_marks_the_statement_card_decided(tmp_path):
    """The other half: answering the richer card answers the whole check, so
    the thin card is decided too and never asks again."""
    d = tmp_path / "d"
    csv = _twin_statement(tmp_path)
    _run(d, _evidence_file(tmp_path), bank_csv=str(csv))
    statement_card = _cards(d, DP_CARD)[0]["id"]
    ev = _evidence_file(tmp_path, TWIN_FEED)
    _run(d, ev, bank_csv=str(csv))
    feed_card = [c for c in _cards(d, DP_CARD) if c["params"]["source"] == "qbo"][0]["id"]

    _approve(d, feed_card)
    _run(d, ev, bank_csv=str(csv))

    by_id = {c["id"]: c for c in _cards(d, DP_CARD)}
    assert by_id[statement_card]["status"] == "rejected", "the thin card is answered too"
    assert str(feed_card) in by_id[statement_card]["params"]["superseded_by"]
    assert by_id[feed_card]["status"] == "approved"
    (event,) = _events(d, SUPERSEDED_EVENT)
    assert event["payload"]["card_id"] == statement_card
    assert event["payload"]["superseded_by"] == feed_card

    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        rows = store.invoices_by_number(ledger, "demo", "DP-Purchase-285")
    assert len(rows) == 1, "one payable row for one physical check"
    assert rows[0]["amount_cents"] == TWIN_CENTS

    last = _run(d, ev, bank_csv=str(csv), direct_payment_floor_cents="1")
    assert len(_cards(d, DP_CARD)) == 2, "and nothing re-asks"
    assert len(_events(d, SUPERSEDED_EVENT)) == 1
    # A resolved card swallowing a fresh ask is an anomaly by design (#161).
    # Nothing re-enqueues either card, so the run is silent.
    assert last.anomalies == []


# --------------------------------------------------------------------------
# The load seam's one invariant (pinned 2026-09-21, ahead of the
# _reconcile_run split): the cross-source indexes are read off the WHOLE
# feed window, before the explained partition removes anything. The third
# clause of the pairing rule is an absence ("this number is on no other feed
# record"), and an absence cannot be read off a filtered list: an
# already-explained record still holds the number it holds.
# --------------------------------------------------------------------------


def test_the_cross_source_indexes_read_the_whole_feed_not_the_filtered_evidence(tmp_path):
    from core.agents.ap.jobs import _reconcile_load
    from core.engine.config import load_tenant
    from core.engine.contracts import JobContext

    d = tmp_path / "d"
    # Run 1: the accounting side of check 4051 settles a row, so that record
    # is ANSWERED (an ap.reconcile.paid event names it) and the next run's
    # decision loop never sees it again.
    numbered = dict(
        TWIN_FEED,
        qbo_id="Purchase:300",
        payee="Acme Tooling",
        check_ref=TWIN_CHECK,
        accounts=[],
    )
    _seed_row(d, vendor="Acme Tooling", number="N-1", cents=TWIN_CENTS, check_ref=TWIN_CHECK)
    _run(d, _evidence_file(tmp_path, numbered))
    assert len(_events(d, "ap.reconcile.paid")) == 1

    # Run 2's window: the answered record again, plus a numberless payment
    # for the same cents on the same day.
    window = tmp_path / "window.json"
    window.write_text(json.dumps([numbered, TWIN_FEED]))
    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        ctx = JobContext(
            tenant=load_tenant("demo"),
            tenant_slug="demo",
            ledger=ledger,
            agent="ap",
            job="reconcile",
            params={"evidence_file": str(window)},
            run_key="demo.ap.reconcile.pin",
        )
        evidence, feed_numbers, feed_numberless, *_ = _reconcile_load(ctx)

    assert [ev.qbo_id for ev in evidence] == [TWIN_FEED["qbo_id"]], "the answered id is out"
    assert feed_numbers == [TWIN_CHECK], "but its number is still on the index"
    assert [p.ident for p in feed_numberless] == [TWIN_FEED["qbo_id"]]

    # Why it matters: on the whole-feed index the statement line for 4051
    # refuses to pair with the numberless payment (Purchase:300 IS its
    # accounting side); on the filtered list's numbers it would pair, and a
    # numberless payment would falsely supersede a card about different money.
    line = dp.Payment("stmt:1", TWIN_CENTS, TWIN_DATE, check_ref=TWIN_CHECK)
    assert dp.cross_source_pairs([line], feed_numberless, feed_numbers=feed_numbers) == {}
    filtered_numbers = [str(ev.check_ref) for ev in evidence if ev.check_ref]
    assert dp.cross_source_pairs([line], feed_numberless, feed_numbers=filtered_numbers) == {
        "stmt:1": TWIN_FEED["qbo_id"]
    }


def test_the_summary_counts_the_statement_lines(tmp_path):
    """Pinned 2026-09-21 after an adversarial read of the split caught the
    statement tier returning nothing: the summary's ``statement lines: N``
    suffix is the only place a morning reader sees that the bank's own file
    was read at all, and no eval had ever asserted it."""
    d = tmp_path / "d"
    csv = _statement(tmp_path, (CHECK_DATE, f"CHECK {CHECK}", CHECK, "-2431.25"))

    result = _run(d, _evidence_file(tmp_path), bank_csv=str(csv))

    assert "statement lines: 1" in result.summary
