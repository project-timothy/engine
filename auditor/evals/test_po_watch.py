"""PO-watch lens evals: every archived customer PO reaches its two homes.

The live shape (2026-08-12): three POs sat captured in the landing archive
for 2+ months while the canonical PO folder and the register's Open-POs
sheet knew nothing — routing went stale silently. The lens joins on the PO
number from the filename alone; filing and the register row stay human acts.
"""

from __future__ import annotations

from openpyxl import Workbook

from auditor.lenses import po_watch

from .fixtures import make_context, make_ledger


def _register(path, numbers, *, sheet="Open POs"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(["ACME OPEN PURCHASE ORDERS"])
    ws.append(["blurb line"])
    ws.append([])
    ws.append(["Customer PO #", "PN", "Project"])
    for n in numbers:
        ws.append([n, "P00_0000", "Some Project"])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))


def _world(tmp_path, *, archived=(), home=(), register_rows=(), register=True):
    make_ledger(tmp_path / "ledger")
    archive = tmp_path / "archive" / "2026-08"
    archive.mkdir(parents=True)
    for name in archived:
        (archive / name).write_bytes(b"pdf")
    home_dir = tmp_path / "po-home"
    home_dir.mkdir()
    for name in home:
        target = home_dir / name  # "Buyer2/PO-x.pdf" lands in a customer subfolder
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"pdf")
    register_path = tmp_path / "register.xlsx"
    if register:
        _register(register_path, register_rows)
    return make_context(
        tmp_path / "ledger",
        po_watch_enabled=True,
        po_archive_dir=str(tmp_path / "archive"),
        po_home_dir=str(home_dir),
        po_register_xlsx=str(register_path),
    )


def test_fully_routed_po_is_quiet(tmp_path):
    ctx = _world(
        tmp_path,
        archived=["PO-B100-10000004_v1_20260823.pdf"],
        home=["PO-B100-10000004_v1_20260823.pdf"],
        register_rows=["B100-10000004"],
    )
    with ctx.ledger:
        assert po_watch.check(ctx) == []


def test_archived_only_po_flags_both_homes(tmp_path):
    ctx = _world(tmp_path, archived=["PO-2D-10000001_v1_20260526.pdf"])
    with ctx.ledger:
        findings = po_watch.check(ctx)
    assert [(f.subject, f.condition) for f in findings] == [
        ("2D-10000001", "not-in-po-home"),
        ("2D-10000001", "not-in-register"),
    ]
    assert all(f.severity == "WARN" for f in findings)


def test_filed_but_untracked_po_flags_register_only(tmp_path):
    ctx = _world(
        tmp_path,
        archived=["PO-2D-10000002_v1_20260805.pdf"],
        home=["PO-2D-10000002_v1_20260805.pdf"],
        register_rows=["2D-10000001"],
    )
    with ctx.ledger:
        findings = po_watch.check(ctx)
    assert [(f.subject, f.condition) for f in findings] == [("2D-10000002", "not-in-register")]


def test_revision_joins_on_the_number_not_the_filename(tmp_path):
    # A v2 revision arrives; the home holds v1 and the register has the row.
    ctx = _world(
        tmp_path,
        archived=["PO-2D-10000003_v2_20260812.pdf"],
        home=["PO-2D-10000003_v1_20260811.pdf"],
        register_rows=["2D-10000003"],
    )
    with ctx.ledger:
        assert po_watch.check(ctx) == []


def test_unreadable_register_warns_and_still_checks_the_home(tmp_path):
    ctx = _world(tmp_path, archived=["PO-260605JMD_v1_20260605.pdf"], register=False)
    with ctx.ledger:
        findings = po_watch.check(ctx)
    conditions = [f.condition for f in findings]
    assert "not-in-po-home" in conditions
    assert "register-unreadable" in conditions
    assert "not-in-register" not in conditions  # the join was not checkable


def test_absent_section_disables_the_lens(tmp_path):
    make_ledger(tmp_path / "ledger")
    ctx = make_context(tmp_path / "ledger")  # po_watch_enabled defaults False
    with ctx.ledger:
        assert po_watch.check(ctx) == []


def test_customer_subfolder_satisfies_the_home_check(tmp_path):
    """Issue #158: the PO home is the PO ROOT with one folder per customer
    (live shape 2026-08-31: the first second-customer PO). A PO filed in its
    customer subfolder is home; before this the lens saw only the top level,
    so a second customer's PO could never satisfy the join."""
    ctx = _world(
        tmp_path,
        archived=("PO-4500000001_v1_20260831.pdf",),
        home=("Buyer2/PO-4500000001_v1_20260831.pdf",),
        register_rows=("4500000001",),
    )
    assert po_watch.check(ctx) == []


def test_staging_subfolders_do_not_satisfy_the_home_check(tmp_path):
    """Underscore folders (_inbound, _quotes) are staging, not filing: a PO
    sitting there has NOT reached its canonical home and must still warn."""
    ctx = _world(
        tmp_path,
        archived=("PO-777-123_v1_20260901.pdf",),
        home=("_inbound/PO-777-123_v1_20260901.pdf",),
        register_rows=("777-123",),
    )
    findings = po_watch.check(ctx)
    assert [f.condition for f in findings] == ["not-in-po-home"]


def test_configured_archive_dir_that_is_missing_warns(tmp_path):
    """Honesty audit 2026-09-03 (04-F4): a configured archive that is absent
    read as 'nothing captured yet' and went green."""
    make_ledger(tmp_path / "ledger")
    ctx = make_context(
        tmp_path / "ledger",
        po_watch_enabled=True,
        po_archive_dir=str(tmp_path / "gone-archive"),
        po_home_dir=str(tmp_path / "home"),
        po_register_xlsx=str(tmp_path / "register.xlsx"),
    )
    with ctx.ledger:
        findings = po_watch.check(ctx)
    assert [f.condition for f in findings] == ["tree-missing"]
    assert findings[0].severity == "WARN"
    assert "gone-archive" in findings[0].detail


def test_missing_po_home_is_named_instead_of_flagging_every_po(tmp_path):
    """04-F4: with the PO home gone, each PO used to print 'has no copy' (a
    claim about a folder that was never read). Name the missing tree once and
    leave the home half unchecked, the register-unreadable shape."""
    ctx = _world(
        tmp_path,
        archived=["PO-2D-10000001_v1_20260526.pdf"],
        register_rows=["2D-10000001"],
    )
    import shutil

    shutil.rmtree(ctx.tenant.po_home_dir)
    with ctx.ledger:
        findings = po_watch.check(ctx)
    assert [f.condition for f in findings] == ["tree-missing"]
    assert "po-home" in findings[0].detail
