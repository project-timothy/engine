"""Vendor-1099 lens evals: the registry's 1099 picture vs QBO's flags.

The live shape (docs/w9-1099-design.md, 2026-07-28): two subcontractors were
1099-due by the CPA appendix while their QBO vendor records carried
Vendor1099 = false, so QBO's January 1099 module would have skipped them.
The lens joins the appendix's taxpayer units to QBO vendors by registry name
+ ledger_aliases and flags drift in either direction. A fake client stands
in for QBO; no eval opens a socket.
"""

from __future__ import annotations

import json
from pathlib import Path

from auditor.clients.qbo import PAGE, AuditorQboClient
from auditor.lenses import vendor_1099

from .fixtures import add_invoice, make_context, make_ledger

_REGISTRY = """
[hollow_oak]
vendor = "Hollow-Oak Welding"
cost_type = "Subcontractors"
w9 = true
tax_classification = "individual/sole-prop"

[northwind]
vendor = "Northwind Fab LLC"
cost_type = "Subcontractors"
w9 = true

[kestrel]
vendor = "Kestrel Design and Graphics"
cost_type = "Freelancers"
w9 = true
tax_classification = "s-corp"
ledger_aliases = ["Kestrel Design and Graphics, Inc"]

[bluefin]
vendor = "Bluefin Solutions LLC"
cost_type = "Subcontractors"
w9 = true
tax_classification = "individual/sole-prop"
tax_recipient = "Sam Bluefin Reed"
ledger_aliases = ["Sam Reed"]

[office]
vendor = "Paperclip Supply"
cost_type = "Supplies"
"""


class FakeQbo:
    def __init__(self, vendors):
        self.vendors = vendors
        self.calls = 0

    def fetch_vendors(self):
        self.calls += 1
        return list(self.vendors)


def _vendor(id_, name, flag, active=True):
    return {"id": str(id_), "display_name": name, "active": active, "vendor_1099": flag}


def _world(tmp_path, *, paid=(), registry=_REGISTRY, enabled=True):
    conn = make_ledger(tmp_path / "ledger")
    for vendor, cents in paid:
        add_invoice(conn, vendor=vendor, amount_cents=cents, status="Paid")
    tenants = tmp_path / "tenants"
    (tenants / "t").mkdir(parents=True)
    (tenants / "t" / "vendors.toml").write_text(registry, encoding="utf-8")
    return make_context(
        tmp_path / "ledger",
        tenants_dir=tenants,
        vendor_1099_enabled=enabled,
        qbo_token_file="~/nonexistent-tokens.json",
    )


def _check(ctx, vendors):
    with ctx.ledger:
        return vendor_1099.check(ctx, client=FakeQbo(vendors))


def _conditions(findings):
    return sorted((f.subject, f.condition) for f in findings)


def test_due_taxpayer_with_flag_on_is_quiet(tmp_path):
    ctx = _world(tmp_path, paid=[("Hollow-Oak Welding", 120_000)])
    assert _check(ctx, [_vendor(23, "Hollow-Oak Welding", True)]) == []


def test_due_taxpayer_with_flag_off_is_flag_missing(tmp_path):
    # $2,500 clears the 2026 floor ($2,000, One Big Beautiful Bill Act; issue #371)
    ctx = _world(tmp_path, paid=[("Hollow-Oak Welding", 250_000), ("Northwind Fab LLC", 543_800)])
    findings = _check(
        ctx, [_vendor(23, "Hollow-Oak Welding", False), _vendor(9, "Northwind Fab LLC", False)]
    )
    assert _conditions(findings) == [
        ("Hollow-Oak Welding", "flag-missing"),
        ("Northwind Fab LLC", "flag-missing"),
    ]
    ricks = next(f for f in findings if f.subject == "Hollow-Oak Welding")
    assert ricks.severity == "WARN"
    assert "id 23" in ricks.detail and "$2,500.00" in ricks.detail
    assert "Track payments for 1099" in ricks.detail


def test_exempt_corp_with_flag_on_is_flag_set_on_exempt(tmp_path):
    ctx = _world(tmp_path, paid=[("Kestrel Design and Graphics", 2_681_800)])
    findings = _check(ctx, [_vendor(12, "Kestrel Design and Graphics, Inc", True)])
    assert _conditions(findings) == [("Kestrel Design and Graphics", "flag-set-on-exempt")]
    assert "s-corp" in findings[0].detail


def test_exempt_corp_with_flag_off_is_quiet_and_alias_matches_the_inc_spelling(tmp_path):
    ctx = _world(tmp_path, paid=[("Kestrel Design and Graphics", 2_681_800)])
    # the QBO record carries the ', Inc' the registry lists as a ledger_alias
    assert _check(ctx, [_vendor(12, "Kestrel Design and Graphics, Inc", False)]) == []


def test_due_taxpayer_with_no_qbo_vendor_is_unmatched(tmp_path):
    ctx = _world(tmp_path, paid=[("Northwind Fab LLC", 543_800)])
    findings = _check(ctx, [_vendor(23, "Hollow-Oak Welding", True)])
    assert _conditions(findings) == [("Northwind Fab LLC", "qbo-vendor-unmatched")]
    assert "'Northwind Fab LLC'" in findings[0].detail


def test_alias_paid_rows_fold_to_the_taxpayer_and_recipient_rides_the_detail(tmp_path):
    # Paid under the historical spelling; the registry folds it to SRR and
    # the 1099 recipient (the W-9 line-1 owner) is named, never a TIN.
    ctx = _world(tmp_path, paid=[("Sam Reed", 5_193_013)])
    findings = _check(ctx, [_vendor(14, "Bluefin Solutions LLC", False)])
    assert _conditions(findings) == [("Bluefin Solutions LLC", "flag-missing")]
    assert "Sam Bluefin Reed" in findings[0].detail


def test_under_the_floor_and_non_1099_cost_types_are_not_candidates(tmp_path):
    ctx = _world(tmp_path, paid=[("Hollow-Oak Welding", 59_999), ("Paperclip Supply", 900_000)])
    fake = FakeQbo(
        [_vendor(23, "Hollow-Oak Welding", False), _vendor(40, "Paperclip Supply", False)]
    )
    with ctx.ledger:
        assert vendor_1099.check(ctx, client=fake) == []
    assert fake.calls == 0  # no candidates: QBO is never asked


def test_name_match_is_case_and_punctuation_insensitive_but_never_fuzzy(tmp_path):
    ctx = _world(tmp_path / "a", paid=[("Hollow-Oak Welding", 250_000)])
    assert _check(ctx, [_vendor(23, "HOLLOW-OAK  WELDING.", True)]) == []
    ctx = _world(tmp_path / "b", paid=[("Hollow-Oak Welding", 250_000)])
    findings = _check(ctx, [_vendor(23, "Hollow Oak Welding", True)])  # hyphen dropped: no match
    assert _conditions(findings) == [("Hollow-Oak Welding", "qbo-vendor-unmatched")]


def test_disabled_or_no_token_is_silent(tmp_path):
    ctx = _world(tmp_path, paid=[("Hollow-Oak Welding", 120_000)], enabled=False)
    assert _check(ctx, [_vendor(23, "Hollow-Oak Welding", False)]) == []
    ctx2 = _world(tmp_path / "b", paid=[("Hollow-Oak Welding", 120_000)])
    ctx2 = make_context(
        tmp_path / "b" / "ledger", tenants_dir=tmp_path / "b" / "tenants", vendor_1099_enabled=True
    )  # qbo_token_file left empty
    assert _check(ctx2, [_vendor(23, "Hollow-Oak Welding", False)]) == []


# ---- the client: vendor fetch pages past QBO's 1000-row cap --------------------


def _token_file(tmp_path: Path) -> Path:
    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps(
            {
                "client_id": "id",
                "client_secret": "secret",
                "refresh_token": "r0",
                "realm_id": "123",
            }
        )
    )
    return path


def test_fetch_vendors_pages_with_startposition(tmp_path):
    pages = {
        1: [{"Id": str(i), "DisplayName": f"V{i}", "Vendor1099": i % 2 == 0} for i in range(PAGE)],
        1 + PAGE: [{"Id": "x", "DisplayName": "Last", "Active": False, "Vendor1099": True}],
    }
    seen: list[str] = []

    def transport(url, *, data, headers):
        if "oauth" in url:
            return {"access_token": "a", "refresh_token": "r1"}
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(url).query)["query"][0]
        seen.append(query)
        start = int(query.split("STARTPOSITION ")[1].split()[0])
        return {"QueryResponse": {"Vendor": pages.get(start, [])}}

    client = AuditorQboClient(_token_file(tmp_path), transport=transport)
    vendors = client.fetch_vendors()
    assert len(vendors) == PAGE + 1
    assert vendors[-1] == {"id": "x", "display_name": "Last", "active": False, "vendor_1099": True}
    assert vendors[1]["vendor_1099"] is False and vendors[2]["vendor_1099"] is True
    assert len(seen) == 2 and all("TaxIdentifier" not in q for q in seen)
