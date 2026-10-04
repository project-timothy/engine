"""Security review 2026-10-03 (finding 4, HIGH): a filed name is one segment.

The invoice number is read from a document by a model, so it is attacker-
chosen text. `_filed_dest_name` flattened slashes in the vendor and not in the
number, and `apply` files with no approval step, so an invoice numbered
``1/../../../statements/x`` was copied outside the filing tree (reproduced with
the engine's own `_dest_for` + `place_copy`). Every component of the filed
name is now one path segment, and the write is confined to the filing tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agents.ap.jobs import FilingEscape, _file_invoice_copy, _filed_dest_name
from core.engine.guard import WriteGuard

_BASE = {
    "vendor": "Acme",
    "invoice_number": "777",
    "amount_cents": 500,
    "invoice_date": "2026-07-01",
    "file": "Invoice_777.pdf",
}

_HOSTILE_NUMBERS = [
    "1/../../../statements/x",
    "..\\..\\statements\\x",
    "/etc/passwd",
    "a\x00b",
    "line\nbreak",
]


@pytest.mark.parametrize("number", _HOSTILE_NUMBERS)
def test_a_hostile_invoice_number_stays_one_segment(number):
    name = _filed_dest_name({**_BASE, "invoice_number": number})
    assert "/" not in name and "\\" not in name
    assert "\x00" not in name and "\n" not in name
    assert Path(name).name == name


@pytest.mark.parametrize("vendor", ["../evil", "..", ".hidden", "a\\b"])
def test_a_hostile_vendor_never_climbs_or_hides(vendor):
    name = _filed_dest_name({**_BASE, "vendor": vendor})
    assert "/" not in name and "\\" not in name
    assert not name.startswith(".")


def test_an_ordinary_slashed_number_flattens_instead_of_making_subfolders():
    name = _filed_dest_name({**_BASE, "invoice_number": "INV/2026/01"})
    assert name == "Acme - Inv INV-2026-01 - $5.00.pdf"


def test_the_reproduced_attack_files_inside_the_tree(tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "Invoice_777.pdf").write_bytes(b"%PDF crafted")
    filing = tmp_path / "filing"
    hostile = {**_BASE, "invoice_number": "1/../../../statements/x"}

    dest, copied = _file_invoice_copy(
        hostile, landing_dir=landing, filing_dir=filing, guard=WriteGuard([]), shadow=False
    )

    assert copied is True
    assert dest.resolve().is_relative_to(filing.resolve())
    assert not (tmp_path / "statements").exists()


def test_a_month_template_that_escapes_is_refused(tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "Invoice_777.pdf").write_bytes(b"%PDF")
    filing = tmp_path / "filing"

    with pytest.raises(FilingEscape):
        _file_invoice_copy(
            _BASE,
            landing_dir=landing,
            filing_dir=filing,
            guard=WriteGuard([]),
            shadow=False,
            month_template="../../{month}",
        )
    assert not any(tmp_path.glob("*.pdf"))


def test_apply_files_a_hostile_row_inside_the_tree_and_refuses_an_escape(tmp_path):
    """End to end through the runner: the hostile number files inside the
    tree, an escaping destination is an anomaly, and the other row still files."""
    import hashlib
    from unittest.mock import patch

    from core.agents.ap import jobs as ap_jobs
    from core.agents.ap import store
    from core.engine.runner import resolve_ledger_root, run
    from core.ledger import Ledger

    landing = tmp_path / "landing"
    landing.mkdir()
    filing = tmp_path / "filing"
    ledger_dir = tmp_path / "data"
    params = {"landing_dir": str(landing), "filing_dir": str(filing)}
    rows = (("1/../../../statements/x", b"%PDF hostile"), ("888", b"%PDF ok"), ("999", b"%PDF esc"))
    with Ledger.open(resolve_ledger_root("demo", ledger_dir)) as ledger:
        for i, (number, body) in enumerate(rows):
            (landing / f"in_{i}.pdf").write_bytes(body)
            store.insert_invoice(
                ledger,
                tenant="demo",
                vendor="Acme",
                invoice_number=number,
                amount_cents=500,
                invoice_date="2026-07-01",
                source_file=f"in_{i}.pdf",
                source_md5=hashlib.md5(body).hexdigest(),
                shadow=True,
            )
    real = ap_jobs._dest_for

    def _escaping(p, filing_dir, template):
        if p["invoice_number"] == "999":
            return filing_dir / ".." / "outside.pdf"
        return real(p, filing_dir, template)

    with patch.object(ap_jobs, "_dest_for", _escaping):
        result = run("demo", "ap", "apply", params=params, ledger_dir=ledger_dir)

    assert result.status == "ok"
    (refused,) = [a for a in result.anomalies if a.code == "ap.filing_refused"]
    assert "999" in refused.detail
    assert not (tmp_path / "outside.pdf").exists()
    assert not (tmp_path / "statements").exists()
    filed = sorted(p.name for p in filing.rglob("*.pdf"))
    assert filed == [
        "Acme - Inv 1-..-..-..-statements-x - $5.00.pdf",
        "Acme - Inv 888 - $5.00.pdf",
    ]
