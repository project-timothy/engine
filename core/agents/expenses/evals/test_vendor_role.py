"""Vendor-role evals (issue #120, owner decision 2026-08-14).

The decision under encoding: a vendor person submits receipts through the
same drop tree, but the booking treatment differs from an owner's in three
ways — the accounting-system payee is the VENDOR RECORD behind the person
(never the person's own name), every line books to the project's chart
account resolved from ``project_account_template`` (project costs, no
overhead accounts, the global ``category_accounts`` table is never
consulted), and the confirm card carries the person's own payment channel.

Contract under test:
- The Purchase books to the mapped vendor with one line per project, each
  line on the project's templated chart account.
- A project whose chart account is missing (or an unset template) parks a
  card, never a guess, never a fallback to category accounts.
- The person-level ``default_channel`` override rides the confirm card.
- Safety-net evidence matches under the VENDOR display name.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.agents.expenses import jobs as exp_jobs
from core.engine.config import load_tenant
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

VENDOR_PERSON = "Vic Vendor"

# The demo's own [close].bank_account: the demo tenant is rendered from the
# archetype template (row 7.19), so a spelled-out name drifts on a re-render.
BANK = load_tenant("demo").close.bank_account
VENDOR_RECORD = "Vic Vendor LLC"


class FakeQbo:
    def __init__(self):
        self.vendors = [
            {"id": "V1", "display_name": "Pat Owner"},
            {"id": "V9", "display_name": VENDOR_RECORD},
        ]
        self.accounts = [
            {"id": "BANK", "fully_qualified_name": BANK},
            {"id": "PC1", "fully_qualified_name": "Project Costs - PN26_2001"},
            {"id": "PC2", "fully_qualified_name": "Project Costs - PN26_2002"},
        ]
        self.recent: list[dict] = []
        self.evidence: list = []
        self.created: list[dict] = []

    def fetch_vendors(self):
        return self.vendors

    def fetch_accounts(self):
        return self.accounts

    def fetch_recent_txns(self, *, since):
        return self.recent

    def fetch_evidence(self, *, since):
        return self.evidence

    def create_purchase(self, payload):
        pid = f"P{len(self.created) + 1}"
        self.created.append(payload)
        return {"Id": pid, **payload}

    def get_purchase(self, purchase_id):
        idx = int(purchase_id[1:]) - 1
        payload = dict(self.created[idx])
        payload["TotalAmt"] = sum(line["Amount"] for line in payload["Line"])
        return {"Id": purchase_id, **payload}


@pytest.fixture()
def fake_qbo(monkeypatch):
    fake = FakeQbo()
    monkeypatch.setattr(exp_jobs, "_qbo_write_client", lambda ctx: fake)
    return fake


def _params(tmp_path, **extra):
    return {
        "drop_dir": str(tmp_path / "drop"),
        "filing_dir": str(tmp_path / "filing"),
        "month": "2026-08",
        "extractor": "fixture",
        **extra,
    }


def _run(job, tmp_path, **extra):
    return run(
        "demo", "expenses", job, params=_params(tmp_path, **extra), ledger_dir=tmp_path / "ledger"
    )


def _ledger(tmp_path):
    return Ledger.open(resolve_ledger_root("demo", tmp_path / "ledger"))


def _approve(tmp_path, action_type):
    with _ledger(tmp_path) as ledger:
        for card in ledger.list_approvals("demo", status="pending"):
            if card["action_type"] == action_type:
                ledger.resolve_approval("demo", card["id"], "approved")


def _reports(tmp_path):
    with _ledger(tmp_path) as ledger:
        rows = ledger.conn.execute("SELECT * FROM expense_report ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def _seed_vendor_receipts(tmp_path, *specs):
    """specs: (name, body, amount, category, date, project)"""
    for name, body, _amount, _category, _date, project in specs:
        target = tmp_path / "drop" / VENDOR_PERSON / project
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(body)
    _run("intake", tmp_path)
    for name, _body, amount, category, date, project in specs:
        filed = tmp_path / "filing" / "2026-08" / "receipts" / VENDOR_PERSON / project
        (filed / (name + ".extract.json")).write_text(
            json.dumps(
                {
                    "doc_type": "receipt",
                    "confidence": 0.9,
                    "vendor_name": "Supplier",
                    "amount": amount,
                    "invoice_date": date,
                    "category": category,
                }
            )
        )
    _run("extract", tmp_path)


def _build_approved_vendor_report(tmp_path):
    _seed_vendor_receipts(
        tmp_path,
        ("parts.pdf", b"p", "100.00", "Supplies", "2026-08-01", "P26_2001"),
        ("steel.pdf", b"s", "250.00", "Supplies", "2026-08-02", "P26_2002"),
        ("site lunch.pdf", b"l", "40.00", "Meals", "2026-08-02", "P26_2002"),
    )
    _run("report", tmp_path, person=VENDOR_PERSON)
    _approve(tmp_path, "expenses.report_review")
    return _run("report", tmp_path, person=VENDOR_PERSON)


def test_confirm_card_carries_the_person_channel_override(tmp_path):
    result = _build_approved_vendor_report(tmp_path)

    (confirm,) = [
        a for a in result.approvals_needed if a.action_type == "expenses.reimbursement_record"
    ]
    # the person entry says Check; the tenant default says Transfer
    assert confirm.params["channel"] == "Check"


def test_purchase_books_to_the_vendor_record_per_project(tmp_path, fake_qbo):
    _build_approved_vendor_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")

    result = _run("match", tmp_path)

    assert result.status in ("ok", "needs_approval")
    (payload,) = fake_qbo.created
    # payee is the vendor record behind the person, never the person's name
    assert payload["EntityRef"] == {"value": "V9", "type": "Vendor"}
    # one line per project on the templated project account; meals included —
    # a vendor's costs are project costs, no overhead accounts (owner decision
    # 2026-08-17), and the category table is never consulted
    by_account = {
        line["AccountBasedExpenseLineDetail"]["AccountRef"]["value"]: line["Amount"]
        for line in payload["Line"]
    }
    assert by_account == {"PC1": 100.00, "PC2": 290.00}
    (report,) = _reports(tmp_path)
    assert report["status"] == "Reimbursed-Recorded"


def test_missing_project_account_parks_a_card_never_guesses(tmp_path, fake_qbo):
    fake_qbo.accounts = [a for a in fake_qbo.accounts if a["id"] != "PC2"]
    _build_approved_vendor_report(tmp_path)
    _approve(tmp_path, "expenses.reimbursement_record")

    result = _run("match", tmp_path)

    cards = [
        a for a in result.approvals_needed if a.action_type == "expenses.qbo_map_project_account"
    ]
    (card,) = cards
    assert "P26_2002" in card.params["projects"]
    assert fake_qbo.created == []
    (report,) = _reports(tmp_path)
    assert report["status"] == "Open"


def test_empty_template_resolves_nothing_and_lists_every_project():
    account_ids, missing = exp_jobs._project_accounts("", ["P26_2001"], {})
    assert account_ids == {}
    assert missing == ["P26_2001"]


def test_template_resolution_strips_the_letter_prefix():
    account_map = {"Project Costs - PN26_2001": "PC1"}
    account_ids, missing = exp_jobs._project_accounts(
        "Project Costs - PN{number}", ["P26_2001", ""], account_map
    )
    assert account_ids == {"P26_2001": "PC1"}
    assert missing == ["(no project)"]


def test_safety_net_evidence_matches_under_the_vendor_name(tmp_path, fake_qbo):
    _build_approved_vendor_report(tmp_path)
    # confirm NOT approved: the report stays Open and leg 2 hunts evidence
    fake_qbo.evidence = [SimpleNamespace(qbo_id="E1", payee=VENDOR_RECORD, amount_cents=39000)]

    result = _run("match", tmp_path)

    (confirm,) = [
        a for a in result.approvals_needed if a.action_type == "expenses.reimbursement_record"
    ]
    assert confirm.params["person"] == VENDOR_PERSON
    assert confirm.params["evidence"] == "E1"
    assert confirm.params["channel"] == "Check"
