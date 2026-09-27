"""ap/reconcile: the whole statement folder is evidence (phase 7 row 7.4).

Row 7.3 read the newest CSV export the owner happened to make. On the live
tenant that was one file in nine months, while nine monthly statement PDFs
sat unread in the same folder holding 107 check numbers — including the two
1099 escapes that were found by hand, and a check whose money the ledger had
recorded with no number on the row.

Contract under test:

- ``--param statement_dir`` reads EVERY statement file in the folder, PDFs
  and CSVs alike, through the same tiers row 7.3 built; ``--param bank_csv``
  keeps working exactly as before (its own evals stay green untouched);
- the run key declares the folder by content: a new statement re-executes,
  an unchanged folder (even re-touched) replays;
- a folder that cannot be listed is an anomaly and a run that never
  replays, never a quiet "no statement this morning" (2026-09-13);
- each file lands one informational ``ap.reconcile.statement_lines`` event
  carrying the section counts, so the parsed withdrawals and deposits are on
  record without any behaviour change;
- a check line whose amount and date fit exactly one SETTLED row carrying no
  reference backfills the number onto that row (``ap.reconcile.ref_backfilled``)
  and counts as already recorded; amount and date never settle an OPEN row;
- an unknown statement check line feeds the hand-check lane exactly as QBO
  money-out does, once across both sources.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

from conftest import minimal_pdf
from core.agents.ap import store
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

VENDOR = "Acme Tooling"
OTHER = "Beta Supply"
CARD = "ap.reconcile_review"
DP_CARD = "ap.record_direct_payment"
PAID = "ap.reconcile.paid"
UNKNOWN = "ap.reconcile.unknown"
BACKFILLED = "ap.reconcile.ref_backfilled"
LINES = "ap.reconcile.statement_lines"
PAY_DATE = "2026-09-10"
SEPTEMBER = "Demo checking - 2026-09-30.pdf"
OCTOBER = "Demo checking - 2026-10-31.pdf"
MARKER = "* Indicates gap in check sequence i = Electronic Image s = Substitute Check"
PROJECT_ACCOUNT = "Cost of Goods Sold:Project Expense - PN00_0101"


def _statement_text(
    checks: tuple[tuple[str, str, str], ...] = (),
    withdrawals: tuple[tuple[str, str, str], ...] = (),
    deposits: tuple[tuple[str, str, str], ...] = (),
) -> str:
    """The bank's layout: (number, MM/DD, amount) checks and (MM/DD, amount,
    memo) rows, each section headed by the count and total it must tie to."""
    lines: list[str] = []
    if checks:
        total = sum(Decimal(a.replace(",", "")) for _, _, a in checks)
        lines += [
            f"Checks {len(checks)} checks totaling ${total:,.2f}",
            MARKER,
            "Number Date Paid Amount Number Date Paid Amount Number Date Paid Amount",
            *(f"{n} i {d} {a}" for n, d, a in checks),
        ]
    for label, rows in (("Withdrawals / Debits", withdrawals), ("Deposits / Credits", deposits)):
        if not rows:
            continue
        total = sum(Decimal(a.replace(",", "")) for _, a, _ in rows)
        lines += [
            f"{label} {len(rows)} items totaling ${total:,.2f}",
            "Date Amount Description",
            *(f"{d} {a} {memo}" for d, a, memo in rows),
        ]
    lines += ["Daily Balance Summary", "Date Amount Date Amount Date Amount"]
    return "\n".join(lines)


def _statement(directory: Path, name: str = SEPTEMBER, **sections) -> Path:
    """A real single-page PDF whose text layer is that statement."""
    path = directory / name
    path.write_bytes(minimal_pdf(_statement_text(**sections)))
    return path


def _folder(tmp_path: Path, name: str = SEPTEMBER, **sections) -> Path:
    d = tmp_path / "Statements"
    d.mkdir(exist_ok=True)
    _statement(d, name, **sections)
    return d


def _seed(ledger_dir: Path, *rows):
    """rows: (vendor, number, cents, status, check_ref, payment_id, pay_date)"""
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        for vendor, number, cents, status, check_ref, payment_id, pay_date in rows:
            inv_id, _ = store.insert_invoice(
                ledger,
                tenant="demo",
                vendor=vendor,
                invoice_number=number,
                amount_cents=cents,
                status=status,
                invoice_date="2026-08-20",
            )
            if check_ref or pay_date:
                store.record_payment_details(
                    ledger, invoice_id=inv_id, payment_date=pay_date, check_ref=check_ref
                )
            if payment_id:
                store.record_qbo_ids(ledger, invoice_id=inv_id, payment_id=payment_id)
    return root


def _evidence_file(tmp_path: Path, *entries) -> Path:
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(list(entries)))
    return p


def _run(ledger_dir: Path, evidence: Path, folder: Path | None = None, *, shadow=False, **extra):
    params = {"evidence_file": str(evidence), **extra}
    if folder is not None:
        params["statement_dir"] = str(folder)
    return run("demo", "ap", "reconcile", shadow=shadow, params=params, ledger_dir=ledger_dir)


def _dp_run(ledger_dir, evidence, folder=None, **extra):
    return _run(ledger_dir, evidence, folder, direct_payment_cards="true", **extra)


def _row(root, number):
    with Ledger.open(root) as ledger:
        return store.invoices_by_number(ledger, "demo", number)[0]


def _events(root, event_type):
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


def _cards(root, action_type=CARD, status=None):
    with Ledger.open(root) as ledger:
        return [
            c
            for c in ledger.list_approvals("demo", status=status)
            if c["action_type"] == action_type
        ]


def _engine_check(tmp_path):
    """The 9058 shape: one engine-written payment covering two rows."""
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "N-1", 10000, "Scheduled", "9058", "BillPayment:BP1", PAY_DATE),
        (VENDOR, "N-2", 2500, "Scheduled", "9058", "BillPayment:BP1", PAY_DATE),
    )
    return d, root


# ---- the folder is the evidence ---------------------------------------------


def test_a_pdf_in_the_folder_settles_exactly_as_the_csv_did(tmp_path):
    """Row 7.3's acceptance, arriving as the bank's own monthly file."""
    d, root = _engine_check(tmp_path)
    folder = _folder(tmp_path, checks=(("9058", "09/16", "125.00"),))

    result = _run(d, _evidence_file(tmp_path), folder)

    assert result.status == "ok", result.summary
    for number in ("N-1", "N-2"):
        row = _row(root, number)
        assert row["status"] == "Paid"
        assert row["payment_date"] == "2026-09-16"
        assert row["check_ref"] == "9058"
    paid = _events(root, PAID)
    assert len(paid) == 2
    assert {p["payload"]["evidence"] for p in paid} == {"statement"}
    assert result.anomalies == []


def test_every_file_in_the_folder_is_read(tmp_path):
    """Two statements and a CSV in one run, and every section counted."""
    d, root = _engine_check(tmp_path)
    folder = _folder(tmp_path, checks=(("9058", "09/16", "125.00"),))
    _statement(
        folder,
        OCTOBER,
        withdrawals=(("10/02", "8.46", "DEMO BUREAU OF WORKERS COLUMBUS OH"),),
        deposits=(("10/14", "105,405.07", "DEMO CUSTOMER PAYMENTS"),),
    )
    (folder / "export.csv").write_text(
        "Posted,Memo,Chk,Value\n2026-09-16,CHECK 7777,7777,-800.00\n"
    )

    result = _run(d, _evidence_file(tmp_path), folder)

    assert _row(root, "N-1")["status"] == "Paid", "the September PDF settled the engine's payment"
    assert [a.code for a in result.anomalies] == ["ap.reconcile.unknown_payment"], "the CSV line"
    counts = {e["payload"]["file"]: e["payload"] for e in _events(root, LINES)}
    assert set(counts) == {SEPTEMBER, OCTOBER, "export.csv"}
    assert counts[SEPTEMBER]["checks"] == 1
    assert counts[OCTOBER]["withdrawals"] == 1
    assert counts[OCTOBER]["deposits"] == 1
    assert counts["export.csv"]["checks"] == 1


def test_a_stray_pdf_is_skipped_by_its_own_text(tmp_path):
    """A statement folder collects strays, and the default glob is loose. A
    PDF with no check-grid marker and no section header is some other
    document: named in the log, skipped, never an error."""
    d, root = _engine_check(tmp_path)
    folder = _folder(tmp_path, checks=(("9058", "09/16", "125.00"),))
    (folder / "some drawing.pdf").write_bytes(minimal_pdf("SHEET 1 OF 6\nAS INSTALLED\n"))

    result = _run(d, _evidence_file(tmp_path), folder)

    assert result.anomalies == []
    assert any("not a bank statement" in a for a in result.actions), result.actions
    assert _row(root, "N-1")["status"] == "Paid"


def test_a_statement_that_does_not_tie_is_an_anomaly_never_a_silent_skip(tmp_path):
    """A parse that drops lines makes money that moved look like money that
    never did. The bank prints a count and a total above every section."""
    d, root = _engine_check(tmp_path)
    folder = tmp_path / "Statements"
    folder.mkdir()
    doctored = _statement_text(checks=(("9058", "09/16", "125.00"),)).replace(
        "Checks 1 checks totaling $125.00", "Checks 2 checks totaling $250.00"
    )
    (folder / SEPTEMBER).write_bytes(minimal_pdf(doctored))

    result = _run(d, _evidence_file(tmp_path), folder)

    assert [a.code for a in result.anomalies] == ["ap.reconcile.statement_unparsed"]
    assert "Checks" in result.anomalies[0].detail
    assert _row(root, "N-1")["status"] == "Scheduled", "a half-read file settles nothing"


def test_an_unreadable_folder_is_an_anomaly_and_never_replays(tmp_path):
    """2026-09-13, the regression: macOS gates readdir per program identity
    and the denial is silent. 'I could not look' must never read as 'nothing
    to look at', and the next run must ask again rather than replay."""
    d, root = _engine_check(tmp_path)
    folder = _folder(tmp_path, checks=(("9058", "09/16", "125.00"),))
    folder.chmod(0o311)
    try:
        first = _run(d, _evidence_file(tmp_path), folder)
        second = _run(d, _evidence_file(tmp_path), folder)
    finally:
        folder.chmod(0o755)

    assert [a.code for a in first.anomalies] == ["ap.reconcile.statement_unreadable"]
    assert [a.code for a in second.anomalies] == ["ap.reconcile.statement_unreadable"]
    assert second.status != "noop", "a refused listing must not replay into silence"
    assert _row(root, "N-1")["status"] == "Scheduled"


def test_the_key_declares_the_folder_by_content(tmp_path):
    d, root = _engine_check(tmp_path)
    folder = _folder(tmp_path, checks=(("9058", "09/16", "125.00"),))
    ev = _evidence_file(tmp_path)

    first = _run(d, ev, folder)
    assert first.status == "ok"
    assert _row(root, "N-1")["status"] == "Paid"
    paid = len(_events(root, PAID))

    second = _run(d, ev, folder)  # the rows flipped, so the key moved
    assert second.status == "ok"
    assert len(_events(root, PAID)) == paid
    assert _run(d, ev, folder).status == "noop"
    os.utime(folder / SEPTEMBER, None)
    assert _run(d, ev, folder).status == "noop", "a re-touched file changes no byte"

    _statement(folder, OCTOBER, checks=(("7777", "10/02", "800.00"),))
    fourth = _run(d, ev, folder)
    assert fourth.status == "ok", "a new statement is new evidence"
    assert [a.code for a in fourth.anomalies] == ["ap.reconcile.unknown_payment"]


def test_no_param_at_all_skips_the_tier_with_one_line(tmp_path):
    d, _root = _engine_check(tmp_path)
    result = _run(d, _evidence_file(tmp_path))
    assert result.status == "ok"
    assert result.anomalies == []
    assert any("statement" in a and "skipped" in a for a in result.actions), result.actions


# ---- the reference backfill (the 9066 class) --------------------------------


def test_a_statement_number_backfills_onto_the_settled_row_that_has_none(tmp_path):
    """The live case: the engine had the payment and never had the number.
    The bank always has the number, so the row gets it — and nothing flips,
    because the row was already Paid."""
    d = tmp_path / "d"
    root = _seed(d, (OTHER, "S-1", 331200, "Paid", "", "", "2026-09-14"))
    folder = _folder(tmp_path, checks=(("9066", "09/16", "3,312.00"),))
    ev = _evidence_file(tmp_path)

    result = _run(d, ev, folder)

    assert result.status == "ok", result.summary
    assert result.anomalies == []
    row = _row(root, "S-1")
    assert row["check_ref"] == "Check 9066"
    assert row["payment_date"] == "2026-09-14", "the backfill names the check, not a new date"
    (event,) = _events(root, BACKFILLED)
    assert event["payload"]["invoice_id"] == row["id"]
    assert event["payload"]["check_ref"] == "Check 9066"
    assert event["payload"]["source"] == "statement"
    assert "already recorded: 1" in result.summary
    assert _events(root, UNKNOWN) == []

    second = _run(d, ev, folder, ignore_payees="Nobody")  # a fresh key, so it really re-runs
    assert second.anomalies == []
    assert len(_events(root, BACKFILLED)) == 1, "a backfilled line is never re-answered"


def test_a_settled_row_with_a_different_number_never_matches(tmp_path):
    """#134's boundary: a row carrying its own reference is a different
    physical payment, whatever the amounts agree on."""
    d = tmp_path / "d"
    root = _seed(d, (OTHER, "S-1", 331200, "Paid", "1099", "", "2026-09-14"))
    folder = _folder(tmp_path, checks=(("9066", "09/16", "3,312.00"),))

    result = _run(d, _evidence_file(tmp_path), folder)

    assert [a.code for a in result.anomalies] == ["ap.reconcile.unknown_payment"]
    assert _row(root, "S-1")["check_ref"] == "1099"
    assert _events(root, BACKFILLED) == []


def test_two_settled_rows_that_fit_park_review_rather_than_guess(tmp_path):
    d = tmp_path / "d"
    root = _seed(
        d,
        (OTHER, "S-1", 331200, "Paid", "", "", "2026-09-14"),
        (VENDOR, "S-2", 331200, "Paid", "", "", "2026-09-15"),
    )
    folder = _folder(tmp_path, checks=(("9066", "09/16", "3,312.00"),))

    result = _run(d, _evidence_file(tmp_path), folder)

    assert result.status == "needs_approval"
    (asked,) = result.approvals_needed
    assert asked.action_type == CARD
    assert "carry no reference" in asked.reason
    assert _events(root, BACKFILLED) == []
    assert _row(root, "S-1")["check_ref"] == ""


def test_amount_and_date_never_settle_an_open_row(tmp_path):
    """7.3's rule stands: the check number is the discriminator for
    settling. An open row that merely fits the amount stays open."""
    d = tmp_path / "d"
    root = _seed(d, (OTHER, "O-1", 331200, "Scheduled", "", "", "2026-09-14"))
    folder = _folder(tmp_path, checks=(("9066", "09/16", "3,312.00"),))

    result = _run(d, _evidence_file(tmp_path), folder)

    assert _row(root, "O-1")["status"] == "Scheduled"
    assert [a.code for a in result.anomalies] == ["ap.reconcile.unknown_payment"]
    assert _events(root, BACKFILLED) == []


# ---- the hand-check lane, second source -------------------------------------


def test_an_unknown_statement_check_parks_a_hand_check_card(tmp_path):
    """The bank never loses the check number; the accounting feed does. A
    cleared check no row explains is exactly the 1099 escape shape."""
    d, root = _engine_check(tmp_path)
    folder = _folder(tmp_path, checks=(("3029", "09/16", "2,431.25"),))

    result = _dp_run(d, _evidence_file(tmp_path), folder)

    (card,) = _cards(root, DP_CARD, "pending")
    assert card["params"]["payee"] == ""
    assert card["params"]["check_ref"] == "3029"
    assert card["params"]["amount_cents"] == 243125
    assert card["params"]["qbo_id"].startswith("stmt:")
    (asked,) = [a for a in result.approvals_needed if a.action_type == DP_CARD]
    assert "bank statement check 3029" in asked.reason
    assert asked.reason.rstrip().endswith("--param payee='<name>'")
    assert result.status == "needs_approval"


def test_the_same_check_never_cards_from_both_sources(tmp_path):
    """One physical check clears in the feed AND on the statement. The owner
    answers it once, whichever source saw it first."""
    d, root = _engine_check(tmp_path)
    ev = _evidence_file(
        tmp_path,
        {
            "qbo_id": "Purchase:64",
            "txn_type": "Purchase",
            "payee": "",
            "amount_cents": 243125,
            "date": "2026-09-15",
            "check_ref": "3029",
            "accounts": [PROJECT_ACCOUNT],
        },
    )
    _dp_run(d, ev)
    assert len(_cards(root, DP_CARD)) == 1

    folder = _folder(tmp_path, checks=(("3029", "09/16", "2,431.25"),))
    _dp_run(d, ev, folder)

    assert len(_cards(root, DP_CARD)) == 1, "the statement must not re-ask the same check"


def test_a_line_below_the_floor_never_cards(tmp_path):
    d, root = _engine_check(tmp_path)
    folder = _folder(tmp_path, checks=(("3029", "09/16", "42.85"),))

    assert _dp_run(d, _evidence_file(tmp_path), folder).status == "ok"
    assert _cards(root, DP_CARD) == [], "the floor throttles the first pass"


def test_the_lane_is_off_by_default_for_statement_lines_too(tmp_path):
    d, root = _engine_check(tmp_path)
    folder = _folder(tmp_path, checks=(("3029", "09/16", "2,431.25"),))

    _run(d, _evidence_file(tmp_path), folder)

    assert _cards(root, DP_CARD) == []


def test_shadow_reports_the_card_it_would_park_and_parks_none(tmp_path):
    d, root = _engine_check(tmp_path)
    folder = _folder(tmp_path, checks=(("3029", "09/16", "2,431.25"),))

    result = _dp_run(d, _evidence_file(tmp_path), folder, shadow=True)

    assert _cards(root, DP_CARD) == [], "a dry look never parks a card"
    assert any("would park a hand-check card" in a and "3029" in a for a in result.actions), (
        result.actions
    )


# ---- legacy references: the ledger's own free text names the check ----------
#
# The 2026-09-16 shadow over nine live statements called 29 checks unknown.
# Thirteen of them were already Paid rows whose ``check_ref`` is free text a
# human typed: "Checks 9032 + 9033", "Check 9025 (cleared 4/3)", "5/3 bill
# pay 9053", "Check 1025 (electronic image)". An exact-token matcher cannot
# equal any of those, so the engine would have asked the owner about money
# his own book already explains — and a tier that asks about explained money
# is the nag that gets a lane muted.
#
# All three rules below read SETTLED rows only. None settles anything, none
# writes anything except the backfill that already existed.


def test_a_settled_check_explains_its_line_however_late_it_cleared(tmp_path):
    """Rule 1: reference plus amount on a settled row is the same physical
    check, whatever the dates say. Legacy rows carry import-artifact payment
    dates (an invoice date, a Finder tag), and check numbers do not repeat on
    one account, so the date window has no work to do here."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "L-1", 331200, "Paid", "9066", "", "2026-08-01"))
    folder = _folder(tmp_path, checks=(("9066", "09/16", "3,312.00"),))  # 46 days later

    result = _run(d, _evidence_file(tmp_path), folder)

    assert result.anomalies == []
    assert "already recorded: 1" in result.summary
    assert _events(root, UNKNOWN) == []
    assert _events(root, BACKFILLED) == [], "the row already had the number"
    assert _row(root, "L-1")["payment_date"] == "2026-08-01", "nothing is rewritten"


def test_one_legacy_reference_explains_every_check_it_names(tmp_path):
    """Rule 2: a row paid with two checks carries both numbers in one string.
    Containment is the whole test — no amount, no date — because the only
    outcome is "do not flag", and the row's own amount answers neither line
    on its own."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "L-2", 1792184, "Paid", "Checks 9032 + 9033", "", "2026-05-05"))
    folder = _folder(
        tmp_path,
        checks=(("9032", "09/11", "10,000.00"), ("9033", "09/12", "7,921.84")),
    )

    result = _dp_run(d, _evidence_file(tmp_path), folder)

    assert result.anomalies == []
    assert "already recorded: 2" in result.summary
    assert _cards(root, DP_CARD) == [], "explained money never reaches the hand-check lane"
    assert _events(root, UNKNOWN) == []


def test_a_parenthetical_or_bill_pay_reference_still_names_its_check(tmp_path):
    """The other free-text shapes from the live book."""
    shapes = ("Check 9025 (cleared 4/3)", "5/3 bill pay 9025", "Check 1025 (electronic image)")
    for n, ref in enumerate(shapes):
        d = tmp_path / f"d{n}"
        number = "1025" if "1025" in ref else "9025"
        _seed(d, (VENDOR, "L-3", 423500, "Paid", ref, "", "2026-04-03"))
        folder = tmp_path / f"s{n}"
        folder.mkdir()
        _statement(folder, SEPTEMBER, checks=((number, "09/03", "4,235.00"),))

        result = _run(d, _evidence_file(tmp_path), folder)

        assert result.anomalies == [], f"{ref} names {number}"
        assert "already recorded: 1" in result.summary


def test_a_conflicting_reference_never_explains_the_line(tmp_path):
    """#134's boundary, unmoved: 1099 is not 9066, and containment must not
    turn a different physical payment into an answer."""
    d = tmp_path / "d"
    root = _seed(d, (OTHER, "C-1", 331200, "Paid", "Check 1099", "", "2026-09-14"))
    folder = _folder(tmp_path, checks=(("9066", "09/16", "3,312.00"),))

    result = _run(d, _evidence_file(tmp_path), folder)

    assert [a.code for a in result.anomalies] == ["ap.reconcile.unknown_payment"]
    assert _row(root, "C-1")["check_ref"] == "Check 1099"
    assert _events(root, BACKFILLED) == []


def test_a_channel_word_is_not_a_reference_and_the_number_backfills(tmp_path):
    """Rule 3: "Electronic", "ACH", "Zelle" say HOW the money moved, not
    which instrument. A row carrying one has no number, so the statement's
    number lands on it exactly as it does on an empty ref — and the old word
    survives in the note, because it was somebody's record of something."""
    d = tmp_path / "d"
    root = _seed(d, (OTHER, "E-1", 147200, "Paid", "Electronic", "", "2026-09-12"))
    folder = _folder(tmp_path, checks=(("9021", "09/16", "1,472.00"),))

    result = _run(d, _evidence_file(tmp_path), folder)

    assert result.anomalies == []
    row = _row(root, "E-1")
    assert row["check_ref"] == "Check 9021"
    (event,) = _events(root, BACKFILLED)
    assert event["payload"]["check_ref"] == "Check 9021"
    with Ledger.open(root) as ledger:
        notes = ledger.conn.execute(
            "SELECT notes FROM ap_invoices WHERE id = ?", (row["id"],)
        ).fetchone()["notes"]
    assert "was: Electronic" in notes


def test_none_of_the_three_rules_ever_touches_an_open_row(tmp_path):
    """Settled-rows-only, all three. An open row carrying a legacy reference
    still settles the way 7.3 settles or not at all."""
    d = tmp_path / "d"
    root = _seed(
        d,
        (VENDOR, "O-1", 400000, "Scheduled", "Checks 9032 + 9033", "", "2026-09-10"),
        (OTHER, "O-2", 147200, "Scheduled", "Electronic", "", "2026-09-12"),
    )
    folder = _folder(
        tmp_path,
        checks=(("9032", "09/11", "10,000.00"), ("9021", "09/16", "1,472.00")),
    )

    result = _run(d, _evidence_file(tmp_path), folder)

    assert _row(root, "O-1")["status"] == "Scheduled"
    assert _row(root, "O-1")["check_ref"] == "Checks 9032 + 9033"
    assert _row(root, "O-2")["status"] == "Scheduled"
    assert _row(root, "O-2")["check_ref"] == "Electronic", "no backfill onto an open row"
    assert [a.code for a in result.anomalies] == ["ap.reconcile.unknown_payment"] * 2


def test_a_short_digit_run_in_a_reference_is_never_a_check_number(tmp_path):
    """ "5/3 bill pay 9053" names ONE check. The date fragments in it are not
    references, and a matcher that let them count would have a two-digit
    date silencing a real check line."""
    d = tmp_path / "d"
    root = _seed(d, (VENDOR, "T-1", 140700, "Paid", "5/3 bill pay 9053", "", "2026-06-12"))
    folder = tmp_path / "Statements"
    folder.mkdir()
    (folder / "export.csv").write_text(
        "Posted,Memo,Chk,Value\n2026-09-16,CHECK 3,3,-700.00\n2026-09-17,CHECK 9053,9053,-1407.00\n"
    )

    result = _run(d, _evidence_file(tmp_path), folder)

    assert "already recorded: 1" in result.summary, "9053 is named; check 3 is not"
    (anomaly,) = result.anomalies
    assert "check 3 " in anomaly.detail, anomaly.detail
    assert _row(root, "T-1")["check_ref"] == "5/3 bill pay 9053", "nothing is rewritten"
