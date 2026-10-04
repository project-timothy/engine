"""1099 reporting-floor-by-tax-year evals (issue #371, owner catch 2026-09-28).

The One Big Beautiful Bill Act raised the 1099-NEC/1099-MISC reporting floor
to $2,000 for payments made after 2025-12-31 (inflation-indexed from 2027,
not yet published here). The floor is a LAW fact, not a tenant preference:
it lives in a table keyed by tax year, and each run judges the single tax
year it is computing (the running appendix already scopes to one year via
``tax_year_for`` — see ``_year_end_cpa``'s ``year_clause``), so every payment
counted in one run already shares that run's year.
"""

from __future__ import annotations

from datetime import datetime

from auditor.advisory.facts import compute_facts, form_1099_floor_cents
from auditor.advisory.render import render_advisory
from auditor.ledger_reader import LedgerReader

from .fixtures import add_invoice, make_ledger

_VENDORS = """
["solo.example"]
vendor = "Solo Sub"
cost_type = "Subcontractors"
"""


def _tenants(tmp_path):
    tdir = tmp_path / "tenants" / "t"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "tenant.toml").write_text('[identity]\nslug = "t"\ntimezone = "UTC"\n')
    (tdir / "vendors.toml").write_text(_VENDORS)
    return tmp_path / "tenants"


def _cpa(tmp_path, *, paid_cents, paid_date):
    conn = make_ledger(tmp_path / "ledger")
    add_invoice(
        conn,
        vendor="Solo Sub",
        invoice_number="S1",
        amount_cents=paid_cents,
        status="Paid",
        payment_date=paid_date,
    )
    tenants_dir = _tenants(tmp_path)
    now = datetime.fromisoformat(paid_date + "T06:00:00+00:00")
    with LedgerReader.open(tmp_path / "ledger") as reader:
        facts = compute_facts(reader, slug="t", now=now, tenants_dir=tenants_dir)
    return facts["year-end-cpa"]


def test_1375_dollar_payment_in_2026_is_not_due(tmp_path):
    cpa = _cpa(tmp_path, paid_cents=137_500, paid_date="2026-03-10")
    assert cpa["form_1099_candidates"] == []
    assert cpa["floor_cents"] == 200_000
    assert cpa["tax_year"] == 2026


def test_1375_dollar_payment_in_2025_is_due(tmp_path):
    cpa = _cpa(tmp_path, paid_cents=137_500, paid_date="2025-03-10")
    flagged = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    assert "Solo Sub" in flagged
    assert cpa["floor_cents"] == 60_000
    assert cpa["tax_year"] == 2025


def test_2500_dollar_payment_in_2026_is_due(tmp_path):
    cpa = _cpa(tmp_path, paid_cents=250_000, paid_date="2026-03-10")
    flagged = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    assert "Solo Sub" in flagged
    assert flagged["Solo Sub"]["paid_cents"] == 250_000


def test_form_1099_floor_cents_table_by_year():
    # 2025 and every year before it: $600, not a fallback (a settled rule).
    assert form_1099_floor_cents(2020) == (60_000, False)
    assert form_1099_floor_cents(2025) == (60_000, False)
    # 2026 onward: $2,000 (One Big Beautiful Bill Act).
    assert form_1099_floor_cents(2026) == (200_000, False)
    # a year past the latest published bracket falls back to the latest
    # known year's floor, and says so.
    assert form_1099_floor_cents(2027) == (200_000, True)
    assert form_1099_floor_cents(2031) == (200_000, True)


def test_render_names_the_threshold_used_when_nothing_crosses_it(tmp_path):
    cpa = _cpa(tmp_path, paid_cents=137_500, paid_date="2026-03-10")
    text = render_advisory({"year-end-cpa": cpa}, "quiet")
    assert "$2,000" in text
    assert "2026" in text


def test_render_names_the_threshold_used_when_a_candidate_is_listed(tmp_path):
    cpa = _cpa(tmp_path, paid_cents=137_500, paid_date="2025-03-10")
    text = render_advisory({"year-end-cpa": cpa}, "quiet")
    assert "$600" in text
    assert "2025" in text
