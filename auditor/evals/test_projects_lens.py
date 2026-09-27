"""Orphan-PN lens evals (old A-PJ-001, re-founded 2026-09-04): every PN the
ledger references (AP gl_account and project, expense lines, timesheet
lines) must exist in the project registry; a wrong PN is money on the wrong
project."""

from __future__ import annotations

from auditor.lenses import projects

from .fixtures import (
    add_event,
    add_expense_line,
    add_expense_report,
    add_invoice,
    make_context,
    make_ledger,
)

REGISTRY = (
    '[meta]\nschema_version = "1"\n\n'
    '[[project]]\npn = "P00_0101"\nfriendly_name = "Widget"\n\n'
    '[[project]]\npn = "P26_2025"\nfriendly_name = "Gadget"\n\n'
    '[[project]]\npn = "P26_2034"\n'
)


def _world(tmp_path, *, registry=REGISTRY, **overrides):
    conn = make_ledger(tmp_path / "ledger")
    path = tmp_path / "project-registry.toml"
    if registry is not None:
        path.write_text(registry)
    ctx = make_context(tmp_path / "ledger", registry_path=str(path), **overrides)
    return conn, ctx


def test_every_referenced_pn_in_the_registry_is_quiet(tmp_path):
    conn, ctx = _world(tmp_path, projects_nicknames={"widget 0101": "P00_0101"})
    add_invoice(
        conn, gl_account="Cost of Goods Sold:Project Expense - PN00_0101", project="Widget 0101"
    )
    add_invoice(conn, gl_account="Insurance", project="General/Overhead")
    add_invoice(conn, gl_account="Subcontractor Expense", project="Multi (Widget 0101, x2)")
    rid = add_expense_report(conn)
    add_expense_line(conn, rid, project="P26_2034")
    add_event(
        conn,
        event_type="timesheets.recorded",
        payload={
            "person": "Worker",
            "week_ending": "2026-07-17",
            "lines": [{"project": "P26_2025", "hours": 4}],
        },
    )
    with ctx.ledger:
        assert projects.check(ctx) == []


def test_orphan_pn_names_every_place_it_was_seen(tmp_path):
    conn, ctx = _world(tmp_path)
    a = add_invoice(
        conn,
        vendor="Acme",
        invoice_number="7",
        gl_account="Cost of Goods Sold:Project Expense - PN00_0135",
    )
    b = add_invoice(conn, vendor="Acme", invoice_number="8", project="P00_0135 Pharma")
    rid = add_expense_report(conn, person="Spender")
    add_expense_line(conn, rid, project="P00_0135")
    add_event(
        conn,
        event_type="timesheets.recorded",
        payload={
            "person": "Worker",
            "week_ending": "2026-07-17",
            "lines": [{"project": "p00 0135", "hours": 1}],
        },
    )
    with ctx.ledger:
        findings = projects.check(ctx)
    assert [(f.subject, f.condition, f.severity) for f in findings] == [
        ("P00_0135", "orphan-pn", "WARN")
    ]
    detail = findings[0].detail
    assert f"AP #{a} Acme / 7 (gl_account)" in detail
    assert f"AP #{b} Acme / 8 (project)" in detail
    assert f"expense report #{rid} Spender" in detail
    assert "timesheet Worker w/e 2026-07-17" in detail
    assert "3 registered" in detail


def test_unresolvable_project_text_is_info(tmp_path):
    conn, ctx = _world(tmp_path)
    add_invoice(conn, vendor="Acme", invoice_number="1", project="Some Free Text")
    with ctx.ledger:
        findings = projects.check(ctx)
    assert [(f.subject, f.condition, f.severity) for f in findings] == [
        ("Some Free Text", "unresolved-project", "INFO")
    ]


def test_one_finding_per_pn_however_many_sightings(tmp_path):
    conn, ctx = _world(tmp_path)
    for i in range(8):
        add_invoice(conn, invoice_number=str(i), project="P99_9999")
    with ctx.ledger:
        findings = projects.check(ctx)
    assert len(findings) == 1
    assert "and 3 more" in findings[0].detail


def test_missing_registry_is_one_warn_not_a_flood(tmp_path):
    conn, ctx = _world(tmp_path, registry=None)
    add_invoice(conn, project="P99_9999")
    with ctx.ledger:
        findings = projects.check(ctx)
    assert [f.condition for f in findings] == ["registry-missing"]


def test_unconfigured_or_disabled_is_out_of_scope(tmp_path):
    conn = make_ledger(tmp_path / "ledger")
    add_invoice(conn, project="P99_9999")
    ctx = make_context(tmp_path / "ledger")
    with ctx.ledger:
        assert projects.check(ctx) == []
    conn, ctx = _world(tmp_path / "b", projects_enabled=False)
    add_invoice(conn, project="P99_9999")
    with ctx.ledger:
        assert projects.check(ctx) == []
