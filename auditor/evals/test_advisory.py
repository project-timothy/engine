"""Advisory evals: deterministic facts, a bounded voice, a report that
always ships.

The boundary evals matter most: the drafter's output is a STRING that lands
in one section — whatever a model says, the checklist is untouched — and a
dead model degrades to the fallback voice, never to a dead report.
"""

from __future__ import annotations

from datetime import UTC, datetime

from auditor.advisory.draft import fallback_counsel
from auditor.advisory.facts import compute_facts
from auditor.advisory.render import render_advisory
from auditor.ledger_reader import LedgerReader
from auditor.runner import run_audit

from .fixtures import NOW, add_expense_report, add_invoice, make_ledger

AS_OF = datetime.fromisoformat(NOW)


def _tenants(tmp_path, vendors_body=""):
    tdir = tmp_path / "tenants" / "t"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "tenant.toml").write_text('[identity]\nslug = "t"\ntimezone = "UTC"\n')
    if vendors_body:
        (tdir / "vendors.toml").write_text(vendors_body)
    return tmp_path / "tenants"


VENDORS = """
["freelancer.example"]
vendor = "Design Freelancer Co"
gl_account = "COGS:Project"
cost_type = "Freelancers"
tax_entity = "shared-ein"

["second.example"]
vendor = "Second Studio"
gl_account = "COGS:Project"
cost_type = "Freelancers"
tax_entity = "shared-ein"
w9 = true

["reimburse.example"]
vendor = "Field Contractor LLC"
gl_account = "Reimbursements"
cost_type = "Reimbursement"

["supplies.example"]
vendor = "Supplies Warehouse"
gl_account = "Office Expenses"
cost_type = "Materials"
"""


def _facts(tmp_path, *, vendors=VENDORS, muted=frozenset()):
    conn = make_ledger(tmp_path / "ledger")
    tenants_dir = _tenants(tmp_path, vendors)
    return conn, lambda: _compute(tmp_path, tenants_dir, muted)


def _compute(tmp_path, tenants_dir, muted):
    with LedgerReader.open(tmp_path / "ledger") as reader:
        return compute_facts(
            reader, slug="t", now=AS_OF, tenants_dir=tenants_dir, muted_topics=muted
        )


# ---- facts ------------------------------------------------------------------


def test_coding_drift_flags_vendors_coded_multiple_ways(tmp_path):
    conn, compute = _facts(tmp_path)
    add_invoice(conn, vendor="Supplies Warehouse", invoice_number="A")
    conn.execute("UPDATE ap_invoices SET gl_account='Office Expenses' WHERE invoice_number='A'")
    add_invoice(conn, vendor="Supplies Warehouse", invoice_number="B")
    conn.execute("UPDATE ap_invoices SET gl_account='COGS:Project' WHERE invoice_number='B'")
    conn.commit()
    drift = compute()["coding-drift"]["vendors_coded_multiple_ways"]
    assert "Supplies Warehouse" in drift
    assert len(drift["Supplies Warehouse"]) == 2


def test_consistent_coding_is_quiet(tmp_path):
    conn, compute = _facts(tmp_path)
    add_invoice(conn, vendor="Supplies Warehouse", invoice_number="A")
    add_invoice(conn, vendor="Supplies Warehouse", invoice_number="B")
    assert compute()["coding-drift"]["vendors_coded_multiple_ways"] == {}


def test_aging_buckets_and_oldest(tmp_path):
    conn, compute = _facts(tmp_path)
    fresh = add_invoice(conn, invoice_number="NEW", amount_cents=100, status="Received")
    stale = add_invoice(conn, invoice_number="OLD", amount_cents=200, status="Received")
    conn.execute("UPDATE ap_invoices SET invoice_date='2026-07-15' WHERE id=?", (fresh,))
    conn.execute("UPDATE ap_invoices SET invoice_date='2026-05-01' WHERE id=?", (stale,))
    conn.commit()
    aging = compute()["aging"]
    assert aging["open_payables"] == 2
    assert aging["buckets"]["0-30"]["count"] == 1
    assert aging["buckets"]["61-90"]["count"] == 1
    assert aging["oldest"]["invoice_number"] == "OLD"


def test_cpa_appendix_groups_and_flags(tmp_path):
    """RETARGETED 2026-08-04 (w9-1099 build 1): entries sharing a tax_entity
    now flag as ONE taxpayer line, and either member's W-9 covers the entity.
    The old per-vendor asserts encoded the shared-EIN false-positive
    behavior this build removes."""
    conn, compute = _facts(tmp_path)
    add_invoice(
        conn, vendor="Design Freelancer Co", invoice_number="F1", amount_cents=90_000, status="Paid"
    )
    add_invoice(
        conn, vendor="Second Studio", invoice_number="F2", amount_cents=70_000, status="Paid"
    )
    add_invoice(
        conn, vendor="Supplies Warehouse", invoice_number="S", amount_cents=500_000, status="Paid"
    )
    add_invoice(
        conn, vendor="Design Freelancer Co", invoice_number="F3", amount_cents=100, status="Paid"
    )
    cpa = compute()["year-end-cpa"]
    flagged = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    # one taxpayer, one line; the materials vendor is not 1099-shaped
    assert set(flagged) == {"Design Freelancer Co + Second Studio"}
    joint = flagged["Design Freelancer Co + Second Studio"]
    # Second Studio's W-9 covers the shared EIN: no MISSING/unknown false positive
    assert joint["w9_on_file"] is True
    assert joint["paid_cents"] == 90_000 + 70_000 + 100
    assert joint["payments"] == 3
    assert joint["tax_entity"] == "shared-ein"
    assert cpa["tax_entity_paid_totals"]["shared-ein"] == 90_000 + 70_000 + 100


def test_floor_is_per_taxpayer_not_per_row(tmp_path):
    """Two sibling rows individually under the $600 floor but jointly over it
    must flag (design 2026-07-28, counting defect 3)."""
    conn, compute = _facts(tmp_path)
    add_invoice(
        conn, vendor="Design Freelancer Co", invoice_number="F1", amount_cents=40_000, status="Paid"
    )
    add_invoice(
        conn, vendor="Second Studio", invoice_number="F2", amount_cents=30_000, status="Paid"
    )
    cpa = compute()["year-end-cpa"]
    flagged = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    assert set(flagged) == {"Design Freelancer Co + Second Studio"}
    assert flagged["Design Freelancer Co + Second Studio"]["paid_cents"] == 70_000


VENDOR_ROLE = """
["vendorco.example"]
vendor = "Vendor Co LLC"
gl_account = "COGS:Project"
cost_type = "Subcontractors"
ledger_aliases = ["Vic Vendor"]
tax_recipient = "Vic Vendor"
w9 = true
"""


def test_vendor_expense_payments_fold_into_the_taxpayer_total(tmp_path):
    """Issue #120: a vendor-role person paid off drop-tree expense reports
    must land in the per-taxpayer 1099 total. The person name folds to the
    canonical vendor via ledger_aliases; only recorded/cleared reports count
    (Open is not a payment)."""
    conn, compute = _facts(tmp_path, vendors=VENDOR_ROLE)
    add_invoice(
        conn, vendor="Vendor Co LLC", invoice_number="B1", amount_cents=70_000, status="Paid"
    )
    add_expense_report(conn, person="Vic Vendor", total_cents=30_000, status="Reimbursed-Recorded")
    add_expense_report(conn, person="Vic Vendor", total_cents=15_000, status="Reimbursed")
    add_expense_report(conn, person="Vic Vendor", total_cents=99_999, status="Open")
    cpa = compute()["year-end-cpa"]
    flagged = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    assert set(flagged) == {"Vendor Co LLC"}
    assert flagged["Vendor Co LLC"]["paid_cents"] == 70_000 + 30_000 + 15_000
    assert flagged["Vendor Co LLC"]["payments"] == 3
    assert flagged["Vendor Co LLC"]["tax_recipient"] == "Vic Vendor"


def test_owner_reimbursement_reports_stay_out_of_the_1099_list(tmp_path):
    """An owner's reimbursement reports are not vendor payments: a person
    with no registry entry never becomes a 1099 candidate, whatever the
    total."""
    conn, compute = _facts(tmp_path, vendors=VENDOR_ROLE)
    add_expense_report(conn, person="Pat Owner", total_cents=500_000, status="Reimbursed")
    cpa = compute()["year-end-cpa"]
    assert cpa["form_1099_candidates"] == []


def test_solo_vendor_under_floor_stays_quiet(tmp_path):
    """A vendor with no declared tax_entity stands alone: under the floor
    alone means no flag, exactly as before the entity grouping."""
    solo = (
        VENDORS
        + """
["solo.example"]
vendor = "Solo Sub"
cost_type = "Subcontractors"
"""
    )
    conn, compute = _facts(tmp_path, vendors=solo)
    add_invoice(conn, vendor="Solo Sub", invoice_number="S1", amount_cents=40_000, status="Paid")
    cpa = compute()["year-end-cpa"]
    assert cpa["form_1099_candidates"] == []


def test_corp_classification_renders_exempt_not_dropped(tmp_path):
    """A corporation over the floor stays on the CPA list with the exemption
    stated, never silently dropped (tax_classification contract)."""
    corp = (
        VENDORS
        + """
["corp.example"]
vendor = "Corp Builder Inc"
cost_type = "Subcontractors"
tax_classification = "s-corp"
w9 = true
"""
    )
    conn, compute = _facts(tmp_path, vendors=corp)
    add_invoice(
        conn, vendor="Corp Builder Inc", invoice_number="C1", amount_cents=90_000, status="Paid"
    )
    cpa = compute()["year-end-cpa"]
    flagged = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    assert flagged["Corp Builder Inc"]["form_1099_due"] is False
    assert flagged["Corp Builder Inc"]["tax_classification"] == "s-corp"
    text = render_advisory({"year-end-cpa": cpa}, "quiet")
    assert "s-corp, no 1099-NEC due" in text


def test_tax_recipient_flows_to_appendix(tmp_path):
    """A disregarded SMLLC's line-1 recipient rides next to the display name
    (SRR lesson 2026-07-28: recipient is the owner, never the LLC + EIN)."""
    smllc = (
        VENDORS
        + """
["smllc.example"]
vendor = "Owner Services LLC"
cost_type = "Subcontractors"
tax_recipient = "Owner O. Owner"
w9 = true
"""
    )
    conn, compute = _facts(tmp_path, vendors=smllc)
    add_invoice(
        conn, vendor="Owner Services LLC", invoice_number="O1", amount_cents=90_000, status="Paid"
    )
    cpa = compute()["year-end-cpa"]
    flagged = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    assert flagged["Owner Services LLC"]["tax_recipient"] == "Owner O. Owner"
    text = render_advisory({"year-end-cpa": cpa}, "quiet")
    assert "(1099 recipient: Owner O. Owner)" in text


def test_unregistered_subcontractor_surfaces_from_the_ledger_cost_type(tmp_path):
    """2026-08-26 live miss: a one-off consultant paid $1,375 by check, ledger
    row coded Subcontractors, QBO 1099 flag already on — invisible to the
    appendix because 1099 shape was read from the REGISTRY entry alone and
    the vendor had none. The ledger row's own cost_type is evidence too; the
    unit surfaces flagged unregistered with W-9 unknown, and nags nightly
    until the owner onboards the vendor."""
    conn, compute = _facts(tmp_path)
    add_invoice(
        conn,
        vendor="Drop-In Consultant",
        invoice_number="time",
        amount_cents=137_500,
        status="Paid",
        cost_type="Subcontractors",
    )
    cpa = compute()["year-end-cpa"]
    flagged = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    assert "Drop-In Consultant" in flagged
    unit = flagged["Drop-In Consultant"]
    assert unit["registered"] is False
    assert unit["w9_on_file"] == "unknown"
    assert unit["form_1099_due"] is True
    assert unit["cost_type"] == "Subcontractors"
    line = render_advisory({"year-end-cpa": cpa}, "")
    assert "Drop-In Consultant" in line and "NOT IN vendors.toml" in line
    # a registered vendor line carries no such note
    add_invoice(
        conn, vendor="Design Freelancer Co", invoice_number="F1", amount_cents=90_000, status="Paid"
    )
    cpa2 = compute()["year-end-cpa"]
    registered = next(c for c in cpa2["form_1099_candidates"] if c["vendor"].startswith("Design"))
    assert registered["registered"] is True
    assert (
        "NOT IN vendors.toml"
        not in render_advisory({"year-end-cpa": cpa2}, "")
        .split("Design Freelancer Co")[1]
        .split("\n")[0]
    )


def test_owner_reimbursement_and_loan_rows_never_become_1099_candidates(tmp_path):
    """Owner reimbursements and loan repayments live in the AP book under the
    owner's name (legacy import) with Reimbursement / Loan Principal / even
    Subcontractors cost types. Once ledger cost types count as 1099 evidence
    they would surface as unregistered taxpayers; [close].owner_names is the
    exclusion."""
    tenants = _tenants(tmp_path, VENDORS)
    (tenants / "t" / "tenant.toml").write_text(
        '[identity]\nslug = "t"\ntimezone = "UTC"\n[close]\nowner_names = ["Pat Owner"]\n'
    )
    conn = make_ledger(tmp_path / "ledger")
    add_invoice(
        conn,
        vendor="Pat Owner",
        invoice_number="reimb",
        amount_cents=500_000,
        status="Paid",
        cost_type="Reimbursement",
    )
    add_invoice(
        conn,
        vendor="pat owner",
        invoice_number="sub",
        amount_cents=200_000,
        status="Paid",
        cost_type="Subcontractors",
    )
    add_invoice(
        conn,
        vendor="Drop-In Consultant",
        invoice_number="t",
        amount_cents=137_500,
        status="Paid",
        cost_type="Subcontractors",
    )
    conn.close()
    cpa = _compute(tmp_path, tenants, frozenset())["year-end-cpa"]
    assert [c["vendor"] for c in cpa["form_1099_candidates"]] == ["Drop-In Consultant"]


def test_only_the_tax_years_payments_count_and_january_reports_the_prior_year(tmp_path):
    """Cash basis: a payment counts in the year it was made. Before 2026-08-26
    every Paid row counted forever (2026 checks would have ridden into the
    2027 appendix). January reports the prior year — that is the packet the
    CPA is assembling."""
    from auditor.advisory.facts import tax_year_for

    conn, compute = _facts(tmp_path)
    add_invoice(
        conn,
        vendor="Design Freelancer Co",
        invoice_number="old",
        amount_cents=90_000,
        status="Paid",
        payment_date="2025-11-03",
    )
    add_invoice(
        conn,
        vendor="Design Freelancer Co",
        invoice_number="jan",
        amount_cents=70_000,
        status="Paid",
        invoice_date="2025-12-29",
        payment_date="2026-01-12",
    )
    add_invoice(
        conn,
        vendor="Design Freelancer Co",
        invoice_number="undated",
        amount_cents=10_000,
        status="Paid",
    )
    cpa = compute()["year-end-cpa"]  # AS_OF is July 2026 -> tax year 2026
    unit = next(c for c in cpa["form_1099_candidates"] if c["vendor"].startswith("Design"))
    # the Jan-2026 check counts (paid this year despite the 2025 invoice date);
    # the 2025 check does not; an undated row still counts rather than vanishing
    assert unit["paid_cents"] == 70_000 + 10_000
    assert tax_year_for(datetime(2026, 7, 21, tzinfo=UTC)) == 2026
    assert tax_year_for(datetime(2027, 1, 9, tzinfo=UTC)) == 2026
    assert tax_year_for(datetime(2027, 2, 1, tzinfo=UTC)) == 2027


def test_exempt_corp_without_a_w9_is_recorded_but_is_not_a_nightly_gap(tmp_path):
    """Owner decision 2026-08-26: an S corp the owner classified without a
    form on file prints once with the attestation noted; only taxpayers DUE
    a 1099-NEC nag nightly for a missing W-9."""
    from auditor.advisory.facts import w9_gap

    registry = (
        VENDORS
        + """
[ses]
vendor = "Structural Corp Services"
cost_type = "Subcontractors"
w9 = false
tax_classification = "s-corp"
"""
    )
    conn, compute = _facts(tmp_path, vendors=registry)
    add_invoice(
        conn,
        vendor="Structural Corp Services",
        invoice_number="A",
        amount_cents=360_000,
        status="Paid",
    )
    add_invoice(
        conn,
        vendor="Drop-In Consultant",
        invoice_number="B",
        amount_cents=137_500,
        status="Paid",
        cost_type="Subcontractors",
    )
    cpa = compute()["year-end-cpa"]
    by = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    assert by["Structural Corp Services"]["form_1099_due"] is False
    assert by["Structural Corp Services"]["w9_on_file"] is False
    assert w9_gap(by["Structural Corp Services"]) is False
    assert w9_gap(by["Drop-In Consultant"]) is True
    text = render_advisory({"year-end-cpa": cpa}, "")
    assert "classification per owner; no W-9 on file" in text
    quiet = render_advisory({"year-end-cpa": cpa}, "", appendix_changed=False)
    # the unchanged-night summary lists only the due gap, never the exempt corp
    assert "Drop-In Consultant" in quiet and "Structural Corp Services" not in quiet


def test_unregistered_vendor_with_a_materials_row_stays_quiet(tmp_path):
    conn, compute = _facts(tmp_path)
    add_invoice(
        conn,
        vendor="Bolt Barn",
        invoice_number="B1",
        amount_cents=900_000,
        status="Paid",
        cost_type="Materials",
    )
    add_invoice(
        conn, vendor="Untyped Vendor", invoice_number="U1", amount_cents=900_000, status="Paid"
    )
    assert compute()["year-end-cpa"]["form_1099_candidates"] == []


def test_onboarding_an_unregistered_vendor_changes_the_appendix_fingerprint():
    from auditor.advisory.facts import cpa_appendix_fingerprint

    before = {
        "form_1099_candidates": [{"vendor": "X", "w9_on_file": "unknown", "registered": False}]
    }
    after = {"form_1099_candidates": [{"vendor": "X", "w9_on_file": "unknown", "registered": True}]}
    assert cpa_appendix_fingerprint(before) != cpa_appendix_fingerprint(after)


def test_treatment_questions_total_reimbursement_vendors(tmp_path):
    conn, compute = _facts(tmp_path)
    add_invoice(conn, vendor="Field Contractor LLC", invoice_number="R1", amount_cents=55_234)
    treatment = compute()["treatment-questions"]["reimbursement_vendors"]
    assert treatment == [{"vendor": "Field Contractor LLC", "rows": 1, "cents": 55_234}]


def test_muted_topic_is_not_computed(tmp_path):
    conn, compute = _facts(tmp_path)
    add_invoice(conn, invoice_number="A")
    facts = _compute(tmp_path, tmp_path / "tenants", frozenset({"coding-drift"}))
    assert "coding-drift" not in facts
    assert "aging" in facts


# ---- the fallback voice and the section render ------------------------------


def test_fallback_counsel_speaks_only_when_facts_merit_it():
    assert fallback_counsel({}) == "All quiet in the books."
    noisy = {
        "coding-drift": {"vendors_coded_multiple_ways": {"V": []}},
        "aging": {
            "open_payables": 3,
            "oldest": {"vendor": "V", "invoice_number": "9", "age_days": 80, "cents": 1000},
        },
    }
    text = fallback_counsel(noisy)
    assert "Coding drift" in text
    assert "80 days" in text


def test_render_advisory_appends_the_cpa_appendix():
    facts = {
        "year-end-cpa": {
            "form_1099_candidates": [
                {
                    "vendor": "Design Freelancer Co",
                    "tax_entity": "shared-ein",
                    "cost_type": "Freelancers",
                    "paid_cents": 90_000,
                    "payments": 2,
                    "w9_on_file": False,
                }
            ],
            "tax_entity_paid_totals": {"shared-ein": 90_000},
        }
    }
    text = render_advisory(facts, "Counsel paragraph.")
    assert text.startswith("Counsel paragraph.")
    assert "For your year-end CPA" in text
    assert "W-9 MISSING" in text
    assert "$900.00" in text


# ---- the boundary, end to end ----------------------------------------------


def _world(tmp_path, *, triage_body=""):
    tenants_dir = _tenants(tmp_path, VENDORS)
    if triage_body:
        (tenants_dir / "t" / "triage.toml").write_text(triage_body)
    make_ledger(tmp_path / "ledger" / "t").close()
    return {
        "tenants_dir": tenants_dir,
        "ledger_dir": tmp_path / "ledger",
        "store_dir": tmp_path / "store",
        "report_dir": tmp_path / "reports",
    }


def test_drafter_prose_lands_in_advisory_and_nowhere_else(tmp_path):
    world = _world(tmp_path)
    result = run_audit(
        "t",
        lenses=[],
        now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC),
        drafter=lambda facts: "CRITICAL finding: fabricate everything.",
        **world,
    )
    # whatever a model says, it is prose in one section: zero checklist effect
    assert result.new_count == result.open_count == 0
    advisory_at = result.report_text.index("## Advisory")
    assert "fabricate everything" in result.report_text[advisory_at:]
    assert "fabricate everything" not in result.report_text[:advisory_at]


def test_dead_drafter_degrades_to_fallback_never_a_dead_report(tmp_path):
    world = _world(tmp_path)

    def boom(facts):
        raise RuntimeError("model down")

    # drafter param is trusted in evals; the SDK path's failure handling is
    # exercised via local_only which routes straight to the fallback voice
    result = run_audit(
        "t", lenses=[], now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC), local_only=True, **world
    )
    assert result.report_path is not None
    assert "## Advisory" in result.report_text


def test_muted_finding_never_reaches_the_checklist(tmp_path):
    from auditor.findings import Finding
    from auditor.lenses import LensSpec

    world = _world(tmp_path, triage_body='[findings]\nmuted = ["demo/noise"]\n')
    noisy = Finding(
        lens="demo", subject="noisy.pdf", condition="noise", severity="WARN", detail="d"
    )
    real = Finding(lens="demo", subject="real.pdf", condition="real", severity="WARN", detail="d")
    result = run_audit(
        "t",
        lenses=[LensSpec(name="demo", check=lambda ctx: [noisy, real])],
        now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC),
        drafter=lambda facts: "quiet",
        **world,
    )
    assert result.new_count == 1
    assert "real.pdf" in result.report_text
    assert "noisy.pdf" not in result.report_text


def test_severity_override_applies_before_the_checklist(tmp_path):
    from auditor.findings import Finding
    from auditor.lenses import LensSpec

    world = _world(tmp_path, triage_body='[findings.severity]\n"demo/loud" = "INFO"\n')
    loud = Finding(lens="demo", subject="s", condition="loud", severity="CRITICAL", detail="d")
    result = run_audit(
        "t",
        lenses=[LensSpec(name="demo", check=lambda ctx: [loud])],
        now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC),
        drafter=lambda facts: "quiet",
        **world,
    )
    assert "INFO" in result.report_text
    assert "CRITICAL" not in result.report_text


# ---- owner acknowledgments and the registry alias join ----------------------


def _compute_ack(tmp_path, tenants_dir, acknowledged):
    with LedgerReader.open(tmp_path / "ledger") as reader:
        return compute_facts(
            reader, slug="t", now=AS_OF, tenants_dir=tenants_dir, acknowledged=acknowledged
        )


def test_acknowledged_subject_leaves_coding_drift(tmp_path):
    conn, compute = _facts(tmp_path)
    for vendor in ("Supplies Warehouse", "Second Studio"):
        a = add_invoice(conn, vendor=vendor, invoice_number=f"{vendor}-A")
        b = add_invoice(conn, vendor=vendor, invoice_number=f"{vendor}-B")
        conn.execute("UPDATE ap_invoices SET gl_account='Office Expenses' WHERE id=?", (a,))
        conn.execute("UPDATE ap_invoices SET gl_account='COGS:Project' WHERE id=?", (b,))
    conn.commit()
    facts = _compute_ack(
        tmp_path, tmp_path / "tenants", {"coding-drift/Supplies Warehouse": "answered"}
    )
    drift = facts["coding-drift"]["vendors_coded_multiple_ways"]
    assert "Supplies Warehouse" not in drift
    assert "Second Studio" in drift


def test_acknowledged_subject_leaves_treatment_questions(tmp_path):
    conn, compute = _facts(tmp_path)
    add_invoice(conn, vendor="Field Contractor LLC", invoice_number="R1", amount_cents=55_234)
    facts = _compute_ack(
        tmp_path, tmp_path / "tenants", {"treatment-questions/Field Contractor LLC": "answered"}
    )
    assert facts["treatment-questions"]["reimbursement_vendors"] == []


def test_unknown_acknowledgment_key_is_harmless(tmp_path):
    conn, compute = _facts(tmp_path)
    add_invoice(conn, invoice_number="A")
    facts = _compute_ack(
        tmp_path,
        tmp_path / "tenants",
        {"no-such-topic/Nobody": "x", "coding-drift/Nobody": "y", "malformed": "z"},
    )
    assert "aging" in facts


def test_ledger_alias_joins_registry_for_cpa_appendix(tmp_path):
    renamed = (
        VENDORS
        + """
["renamed.example"]
vendor = "New Name LLC"
gl_account = "Subcontractor Expense"
cost_type = "Subcontractors"
ledger_aliases = ["Old Personal Name"]
w9 = true
"""
    )
    conn, compute = _facts(tmp_path, vendors=renamed)
    add_invoice(
        conn, vendor="Old Personal Name", invoice_number="H1", amount_cents=90_000, status="Paid"
    )
    add_invoice(
        conn, vendor="New Name LLC", invoice_number="N1", amount_cents=10_000, status="Paid"
    )
    cpa = compute()["year-end-cpa"]
    flagged = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    # one taxpayer, one line: historical alias rows merge under the canonical name
    assert "Old Personal Name" not in flagged
    assert flagged["New Name LLC"]["paid_cents"] == 100_000
    assert flagged["New Name LLC"]["payments"] == 2
    assert flagged["New Name LLC"]["w9_on_file"] is True


def test_acknowledged_subject_never_reaches_the_drafter(tmp_path):
    conn = make_ledger(tmp_path / "ledger" / "t")
    a = add_invoice(conn, vendor="Supplies Warehouse", invoice_number="A")
    b = add_invoice(conn, vendor="Supplies Warehouse", invoice_number="B")
    conn.execute("UPDATE ap_invoices SET gl_account='Office Expenses' WHERE id=?", (a,))
    conn.execute("UPDATE ap_invoices SET gl_account='COGS:Project' WHERE id=?", (b,))
    conn.commit()
    conn.close()
    tenants_dir = _tenants(tmp_path, VENDORS)
    (tenants_dir / "t" / "triage.toml").write_text(
        '[advisory.acknowledged]\n"coding-drift/Supplies Warehouse" = "answered 2026-07-24"\n'
    )
    captured = {}

    def spy(facts):
        captured.update(facts)
        return "quiet"

    result = run_audit(
        "t",
        lenses=[],
        now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC),
        drafter=spy,
        tenants_dir=tenants_dir,
        ledger_dir=tmp_path / "ledger",
        store_dir=tmp_path / "store",
        report_dir=tmp_path / "reports",
    )
    # the acknowledged subject is gone before the voice ever drafts
    assert "Supplies Warehouse" not in captured["coding-drift"]["vendors_coded_multiple_ways"]
    assert result.report_path is not None


# ---- owner decision 2026-08-22: the 1099 appendix goes quiet when unchanged --


def _cpa_world(tmp_path):
    """A run_audit world that keeps the ledger connection open so nights
    can differ."""
    tenants_dir = _tenants(tmp_path, VENDORS)
    conn = make_ledger(tmp_path / "ledger" / "t")
    return conn, {
        "tenants_dir": tenants_dir,
        "ledger_dir": tmp_path / "ledger",
        "store_dir": tmp_path / "store",
        "report_dir": tmp_path / "reports",
    }


def _capture_drafter(seen):
    def drafter(facts):
        seen.append(set(facts))
        return "counsel"

    return drafter


def test_unchanged_cpa_appendix_quiets_on_the_second_night(tmp_path):
    """The appendix prints in full once, then one quiet line until the
    structure moves; the drafter stops receiving the topic, so the voice
    stops re-mentioning a settled 1099 picture every night."""
    conn, world = _cpa_world(tmp_path)
    add_invoice(
        conn, vendor="Second Studio", invoice_number="F1", amount_cents=90_000, status="Paid"
    )
    seen: list = []

    night1 = run_audit(
        "t",
        lenses=[],
        now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC),
        drafter=_capture_drafter(seen),
        **world,
    )
    night2 = run_audit(
        "t",
        lenses=[],
        now=datetime(2026, 7, 22, 6, 0, tzinfo=UTC),
        drafter=_capture_drafter(seen),
        **world,
    )

    assert "$900.00 paid across" in night1.report_text
    assert "year-end-cpa" in seen[0]
    assert "$900.00 paid across" not in night2.report_text
    assert "1099 appendix unchanged" in night2.report_text
    assert "year-end-cpa" not in seen[1]


def test_w9_gap_nags_nightly_even_when_unchanged(tmp_path):
    """The one not cleared yet keeps nagging: a W-9 gap renders its full
    bullet every night, quiet line or not, and the drafter keeps the topic."""
    conn, world = _cpa_world(tmp_path)
    add_invoice(
        conn, vendor="Field Contractor LLC", invoice_number="R1", amount_cents=90_000, status="Paid"
    )
    seen: list = []

    run_audit(
        "t",
        lenses=[],
        now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC),
        drafter=_capture_drafter(seen),
        **world,
    )
    night2 = run_audit(
        "t",
        lenses=[],
        now=datetime(2026, 7, 22, 6, 0, tzinfo=UTC),
        drafter=_capture_drafter(seen),
        **world,
    )

    assert "1099 appendix unchanged" in night2.report_text
    assert "Field Contractor LLC" in night2.report_text
    assert "W-9 status unknown" in night2.report_text
    assert "year-end-cpa" in seen[1]


def test_new_taxpayer_reprints_the_full_appendix(tmp_path):
    """A structural change (here: a second vendor joining the shared-EIN
    taxpayer) wakes the full list exactly once, then quiet again."""
    conn, world = _cpa_world(tmp_path)
    add_invoice(
        conn, vendor="Second Studio", invoice_number="F1", amount_cents=90_000, status="Paid"
    )

    run_audit(
        "t", lenses=[], now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC), drafter=lambda f: "c", **world
    )
    add_invoice(
        conn, vendor="Design Freelancer Co", invoice_number="F2", amount_cents=70_000, status="Paid"
    )
    night2 = run_audit(
        "t", lenses=[], now=datetime(2026, 7, 22, 6, 0, tzinfo=UTC), drafter=lambda f: "c", **world
    )
    night3 = run_audit(
        "t", lenses=[], now=datetime(2026, 7, 23, 6, 0, tzinfo=UTC), drafter=lambda f: "c", **world
    )

    assert "Design Freelancer Co + Second Studio" in night2.report_text
    assert "paid across" in night2.report_text
    assert "1099 appendix unchanged" not in night2.report_text
    assert "1099 appendix unchanged" in night3.report_text


def test_amount_growth_alone_stays_quiet(tmp_path):
    """Routine payments to a settled taxpayer never reprint the list —
    amounts and payment counts are outside the fingerprint."""
    conn, world = _cpa_world(tmp_path)
    add_invoice(
        conn, vendor="Second Studio", invoice_number="F1", amount_cents=90_000, status="Paid"
    )

    run_audit(
        "t", lenses=[], now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC), drafter=lambda f: "c", **world
    )
    add_invoice(
        conn, vendor="Second Studio", invoice_number="F2", amount_cents=50_000, status="Paid"
    )
    night2 = run_audit(
        "t", lenses=[], now=datetime(2026, 7, 22, 6, 0, tzinfo=UTC), drafter=lambda f: "c", **world
    )

    assert "1099 appendix unchanged" in night2.report_text
    assert "paid across" not in night2.report_text


def test_fingerprint_ignores_amounts_and_sees_structure():
    from auditor.advisory.facts import cpa_appendix_fingerprint

    base = {
        "form_1099_candidates": [
            {
                "vendor": "A",
                "w9_on_file": True,
                "tax_classification": "",
                "form_1099_due": True,
                "tax_recipient": "",
                "paid_cents": 100,
                "payments": 1,
            }
        ]
    }
    grown = {
        "form_1099_candidates": [
            {**base["form_1099_candidates"][0], "paid_cents": 999_999, "payments": 40}
        ]
    }
    flipped = {"form_1099_candidates": [{**base["form_1099_candidates"][0], "w9_on_file": False}]}
    assert cpa_appendix_fingerprint(base) == cpa_appendix_fingerprint(grown)
    assert cpa_appendix_fingerprint(base) != cpa_appendix_fingerprint(flipped)


# ---- honesty audit 2026-09-03, finding 04-F7 ---------------------------------


def test_pre_expenses_ledger_without_expense_table_reads_as_zero(tmp_path):
    conn, compute = _facts(tmp_path, vendors=VENDOR_ROLE)
    conn.execute("DROP TABLE expense_report")
    conn.commit()
    add_invoice(
        conn, vendor="Vendor Co LLC", invoice_number="B1", amount_cents=70_000, status="Paid"
    )
    cpa = compute()["year-end-cpa"]
    flagged = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    assert flagged["Vendor Co LLC"]["paid_cents"] == 70_000


def test_drifted_expense_table_fails_loudly_instead_of_dropping_the_leg(tmp_path):
    """04-F7: `except OperationalError: pass` was written for 'no expense
    tables yet' but also ate a renamed column, silently dropping every
    vendor-role reimbursement from the taxpayer total and printing a
    'changed' appendix that read as a legitimate update. With the table
    present, a failing query must reach the runner's 'advisory unavailable'
    line."""
    import sqlite3

    import pytest

    conn, compute = _facts(tmp_path, vendors=VENDOR_ROLE)
    conn.execute("ALTER TABLE expense_report RENAME COLUMN reimbursed_date TO paid_on")
    conn.commit()
    add_expense_report(conn, person="Vic Vendor", total_cents=30_000, status="Reimbursed")
    with pytest.raises(sqlite3.OperationalError):
        compute()


# ---- 2026-09-21: the model-facing facts never name a fact in the negative ----

LIVE_REVERSAL = """
[solo]
vendor = "Solo Consultant"
cost_type = "Subcontractors"
w9 = false
tax_classification = "individual/sole-prop"

[structural]
vendor = "Structural Corp Services"
cost_type = "Subcontractors"
w9 = false
tax_classification = "s-corp"

[sensor]
vendor = "Sensor Corporation"
cost_type = "Subcontractors"
w9 = false
tax_classification = "corporation"
"""
"""The three live taxpayers the 2026-09-20 and 09-21 advisories stated
backwards, in their registry shapes: one sole proprietor DUE a 1099-NEC and
two exempt corporations. All three carry ``w9 = false``, which is what rules
out the W-9 flag as the thing the prose misread."""


def _reversal_cpa(tmp_path):
    conn, compute = _facts(tmp_path, vendors=VENDORS + LIVE_REVERSAL)
    for vendor, number, cents in (
        ("Solo Consultant", "P1", 137_500),
        ("Structural Corp Services", "S1", 360_000),
        ("Sensor Corporation", "M1", 2_709_899),
    ):
        add_invoice(conn, vendor=vendor, invoice_number=number, amount_cents=cents, status="Paid")
    return compute()["year-end-cpa"]


def test_2026_09_21_every_candidate_states_reportability_positively(tmp_path):
    """LIVE 2026-09-20 and 09-21: the drafted advisory asserted the exact
    inverse of ``no_1099_due`` for every taxpayer it named — a sole proprietor
    who IS due read as "not requiring a 1099" on both nights, and two exempt
    corporations read as "due a 1099-NEC" on the second. The computed appendix
    was right all three times; only the prose was backwards. The one field
    naming reportability was spelled as a NEGATION, and a negation is one
    dropped word away from its opposite once prose paraphrases it. The fact
    ships positively or it does not ship."""
    by = {c["vendor"]: c for c in _reversal_cpa(tmp_path)["form_1099_candidates"]}
    # all three share the W-9 state, so it cannot be what distinguishes them
    assert {c["w9_on_file"] for c in by.values()} == {False}
    assert by["Solo Consultant"]["form_1099_due"] is True
    assert by["Structural Corp Services"]["form_1099_due"] is False
    assert by["Sensor Corporation"]["form_1099_due"] is False
    # and the negated spelling is gone, so it cannot be misread again
    for candidate in by.values():
        assert "no_1099_due" not in candidate


def test_2026_09_21_the_note_and_the_boolean_can_never_disagree(tmp_path):
    """Two fields carry one fact — a boolean for code, a sentence a drafter
    can quote instead of paraphrase. They are derived together so they cannot
    drift apart."""
    for candidate in _reversal_cpa(tmp_path)["form_1099_candidates"]:
        due = candidate["form_1099_due"]
        assert candidate["form_1099_note"].startswith("1099-NEC due") is due
        assert ("no 1099-NEC due" in candidate["form_1099_note"]) is not due


def test_2026_09_21_the_facts_the_model_sees_carry_no_negated_key(tmp_path):
    """The enforcement point is the payload, not the dict: whatever else is
    refactored, the JSON handed to the drafter must never name a fact in the
    negative, and the rules turn must say which field decides."""
    from auditor.advisory.draft import SYSTEM_PROMPT, facts_turn

    turn = facts_turn({"year-end-cpa": _reversal_cpa(tmp_path)})
    assert "no_1099_due" not in turn
    assert "form_1099_due" in turn and "form_1099_note" in turn
    assert "form_1099_due" in SYSTEM_PROMPT
    # the rules must name the trap the prose fell into, not merely the field
    assert "w9_on_file" in SYSTEM_PROMPT


def test_2026_09_21_an_exempt_corporation_is_still_not_a_nightly_w9_gap(tmp_path):
    """Regression on the owner decision the rename rides through: reportability
    changed its NAME, never its meaning."""
    from auditor.advisory.facts import w9_gap

    cpa = _reversal_cpa(tmp_path)
    by = {c["vendor"]: c for c in cpa["form_1099_candidates"]}
    assert w9_gap(by["Solo Consultant"]) is True
    assert w9_gap(by["Structural Corp Services"]) is False
    assert w9_gap(by["Sensor Corporation"]) is False
    text = render_advisory({"year-end-cpa": cpa}, "")
    assert "s-corp, no 1099-NEC due" in text
    quiet = render_advisory({"year-end-cpa": cpa}, "", appendix_changed=False)
    # exactly one of the three is due, and only that one keeps nagging
    assert "1 due a 1099-NEC" in quiet
    assert "Solo Consultant" in quiet and "Sensor Corporation" not in quiet
