"""ar/remittance: the customer's remittance advice is money-in evidence (#282).

The incident: a remittance advice for $58,250.00 arrived on a Sunday night,
said which invoice and which credit memo made up the number, and named the
payment date. Nothing in the engine caught it. The AP lane fetches mail, but
it fetches ATTACHMENTS, and a remittance advice carries none: the numbers sit
in two HTML tables in the body. The owner noticed three days later, by the
absence of a line in the morning brief.

Contract under test:

- a message whose subject carries the remittance marker is parsed BODY-ONLY
  into payment number, payment date, payment amount, and the invoice table
  (number, date, description, amount paid, amount remaining);
- one ``ar.remittance.received`` event per payment number, ever: a second
  run over the same mailbox, even with the run key moved, records nothing;
- a credit memo row is money the payment did not carry, so a two-row advice
  parses both rows and the paid amounts tie to the payment amount;
- a statement deposit whose amount equals a received remittance emits
  ``ar.remittance.cleared`` once, and a deposit that matches nothing emits
  nothing at all;
- a body with no table at all is an anomaly, never a silent skip;
- the run writes one note the morning brief can read, and shadow writes
  neither event nor note.

The fixture is the 2026-09-14 advice: payment 400000001, $58,250.00 paid
2026-09-15, one invoice less one credit memo. The money, the payment number
and the invoice references are the real ones; the party names and the sender
are synthetic because everything under ``core/`` is scanned by the
tenant-boundary lint, and the two line descriptions are shortened because the
real ones name a person at the customer.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from conftest import minimal_pdf
from core.agents.ar.schema import parse_remittance
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

RECEIVED = "ar.remittance.received"
CLEARED = "ar.remittance.cleared"
UNPARSED = "ar.remittance.unparsed"

SENDER = "noreply@buyer.example.com"
ACCOUNT = "DEMO TENANT INC:BUYER TECHNOLOGIES:SERVICES(6STLH)"
SUPPLIER_NO = "500000001"
PAYMENT = "400000001"
SUBJECT = f"Remittance Advice - {ACCOUNT} Payment# {PAYMENT}"
AMOUNT = "58,250.00"
AMOUNT_CENTS = 5_825_000
PAID_ON = "15-SEP-2026"
INVOICE = ("0141XYZ", "12-JUN-2026", "Engineering work performed", "", "128,400.00", "0.00")
CREDIT = ("CRE0110XYZ", "01-MAY-2026", "Credit memo applied", "", "-70,150.00", "0.00")
STATEMENT = "Demo checking - 2026-09-30.pdf"
DEPOSIT_MEMO = "BUYER.COM SERVI PAYMENTS FCS 5551234567"


# ---- the mailbox -------------------------------------------------------------


def _remittance_html(
    *,
    payment_number: str = PAYMENT,
    payment_date: str = PAID_ON,
    amount: str = AMOUNT,
    rows: tuple[tuple[str, ...], ...] = (INVOICE,),
) -> str:
    """The buyer's own markup: a label/value table, then the invoice grid."""
    header = "".join(
        f'<tr><td width="160"><b>{label}</b></td><td>{value}</td></tr>'
        for label, value in (
            ("Payment made to:", ACCOUNT),
            ("Our Supplier No.:", SUPPLIER_NO),
            ("Supplier site name:", "USUSD00"),
            ("Payment number:", payment_number),
            ("Payment date:", payment_date),
            ("Payment currency:", "USD"),
            ("Payment amount:", amount),
        )
    )
    columns = (
        "Invoice Number",
        "Invoice Date",
        "Invoice Description",
        "Discount Taken",
        "Amount Paid",
        "Amount Remaining",
    )
    head = "".join(f"<td><u>{column}</u> </td>" for column in columns)
    grid = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return (
        '<html><body bgcolor="#FFFFFF"><br><hr>'
        "<p>*****************PLEASE DO NOT RESPOND TO THIS EMAIL********************</p>"
        "<p>The following payment has been made. It will be paid by bank transfer "
        "directly into your bank account.</p>"
        f"<table><tbody>{header}</tbody></table><br><br>"
        f'<center><table align="CENTER"><tbody><tr>{head}</tr>{grid}</tbody></table></center>'
        "</body></html>"
    )


def _message(
    *,
    id: str = "msg-remit",
    sender: str = SENDER,
    subject: str = SUBJECT,
    date: str = "2026-09-14T20:07:00Z",
    body: str | None = None,
) -> dict:
    return {
        "id": id,
        "sender": sender,
        "subject": subject,
        "date": date,
        "body": _remittance_html() if body is None else body,
    }


def _mailbox(tmp_path: Path, *messages: dict, name: str = "mailbox.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(list(messages)))
    return path


# ---- the bank statement ------------------------------------------------------


def _statement_text(deposits: tuple[tuple[str, str, str], ...]) -> str:
    total = sum(Decimal(a.replace(",", "")) for _, a, _ in deposits)
    return "\n".join(
        [
            f"Deposits / Credits {len(deposits)} items totaling ${total:,.2f}",
            "Date Amount Description",
            *(f"{d} {a} {memo}" for d, a, memo in deposits),
            "Daily Balance Summary",
            "Date Amount Date Amount Date Amount",
        ]
    )


def _folder(tmp_path: Path, *deposits: tuple[str, str, str], name: str = STATEMENT) -> Path:
    directory = tmp_path / "Statements"
    directory.mkdir(exist_ok=True)
    (directory / name).write_bytes(minimal_pdf(_statement_text(deposits)))
    return directory


# ---- running -----------------------------------------------------------------


def _run(
    ledger_dir: Path,
    mailbox: Path,
    *,
    folder: Path | None = None,
    reports: Path | None = None,
    shadow: bool = False,
    **extra,
):
    params = {"messages_file": str(mailbox), **extra}
    if folder is not None:
        params["statement_dir"] = str(folder)
    if reports is not None:
        params["report_dir"] = str(reports)
    return run("demo", "ar", "remittance", shadow=shadow, params=params, ledger_dir=ledger_dir)


def _events(ledger_dir: Path, event_type: str) -> list[dict]:
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


# ---- the advice is read ------------------------------------------------------


def test_a_remittance_advice_lands_one_received_event(tmp_path):
    """The 2026-09-14 message, end to end: the money, the date, the rows."""
    d = tmp_path / "d"
    result = _run(d, _mailbox(tmp_path, _message(body=_remittance_html(rows=(INVOICE, CREDIT)))))

    assert result.status == "ok", result.summary
    events = _events(d, RECEIVED)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["payment_number"] == PAYMENT
    assert payload["payment_date"] == "2026-09-15"
    assert payload["amount_cents"] == AMOUNT_CENTS
    assert payload["currency"] == "USD"
    assert [line["invoice_number"] for line in payload["invoices"]] == ["0141XYZ", "CRE0110XYZ"]
    assert [line["amount_paid_cents"] for line in payload["invoices"]] == [12_840_000, -7_015_000]
    assert sum(line["amount_paid_cents"] for line in payload["invoices"]) == AMOUNT_CENTS
    assert result.anomalies == []


def test_a_second_run_over_the_same_mailbox_records_nothing(tmp_path):
    """Idempotent on the payment number, not on the run key: the second run
    carries an extra message so its key is new, and it still records once."""
    d = tmp_path / "d"
    first = _mailbox(tmp_path, _message())
    _run(d, first)
    second = _mailbox(
        tmp_path,
        _message(),
        _message(id="msg-other", subject="Lunch", body="<p>no tables here</p>"),
        name="mailbox-2.json",
    )
    result = _run(d, second)

    assert result.status == "ok", result.summary
    # Executed, not replayed: the recorded set is in the key, so the second
    # run really looked at the same advice again and declined it.
    assert "1 already recorded" in result.summary
    assert len(_events(d, RECEIVED)) == 1


def test_a_message_that_is_not_a_remittance_is_never_parsed(tmp_path):
    """The subject marker is the gate. No marker, no parse, no event."""
    d = tmp_path / "d"
    result = _run(d, _mailbox(tmp_path, _message(subject="Your order has shipped")))

    assert result.status == "ok", result.summary
    assert _events(d, RECEIVED) == []
    assert result.anomalies == []


def test_a_denied_sender_is_never_parsed_whatever_the_subject_says(tmp_path):
    """The mail lane's privacy boundary is the first gate here too: a denied
    sender's body is never opened, and nothing about it is recorded."""
    d = tmp_path / "d"
    result = _run(
        d,
        _mailbox(tmp_path, _message(sender="billing@clinic.example.org")),
        denied_senders="clinic.example.org",
    )

    assert result.status == "ok", result.summary
    assert _events(d, RECEIVED) == []
    assert result.anomalies == []
    assert not any("clinic" in action for action in result.actions)


def test_an_advice_whose_body_carries_no_table_is_an_anomaly(tmp_path):
    """A shape change must be loud: a remittance the parser cannot read is
    money the engine would otherwise drop on the floor."""
    d = tmp_path / "d"
    result = _run(d, _mailbox(tmp_path, _message(body="<p>See the attached PDF.</p>")))

    assert _events(d, RECEIVED) == []
    assert [a.code for a in result.anomalies] == [UNPARSED]


def test_two_invoice_rows_parse_from_the_body_alone(tmp_path):
    """The parser, directly: one invoice and one credit memo, both read."""
    remittance = parse_remittance(subject=SUBJECT, body=_remittance_html(rows=(INVOICE, CREDIT)))

    assert remittance.payment_number == PAYMENT
    assert remittance.amount_cents == AMOUNT_CENTS
    assert len(remittance.invoices) == 2
    assert remittance.invoices[0].invoice_date == "2026-06-12"
    assert remittance.invoices[0].description == "Engineering work performed"
    assert remittance.invoices[1].amount_paid_cents == -7_015_000
    assert remittance.supplier_no == SUPPLIER_NO


# ---- the bank closes the loop ------------------------------------------------


def test_a_matching_deposit_clears_the_remittance_once(tmp_path):
    """Amount equality against the statement's own Deposits section."""
    d = tmp_path / "d"
    mailbox = _mailbox(tmp_path, _message())
    folder = _folder(tmp_path, ("09/15", AMOUNT, DEPOSIT_MEMO))

    result = _run(d, mailbox, folder=folder)

    assert result.status == "ok", result.summary
    cleared = _events(d, CLEARED)
    assert len(cleared) == 1
    assert cleared[0]["payload"]["payment_number"] == PAYMENT
    assert cleared[0]["payload"]["amount_cents"] == AMOUNT_CENTS
    assert cleared[0]["payload"]["deposit_date"] == "2026-09-15"
    assert cleared[0]["payload"]["statement_file"] == STATEMENT

    again = _run(
        d,
        _mailbox(tmp_path, _message(), _message(id="m2", subject="hello"), name="mb2.json"),
        folder=folder,
    )
    assert again.status == "ok", again.summary
    assert "0 cleared" in again.summary
    assert len(_events(d, CLEARED)) == 1


def test_one_deposit_clears_one_payment_even_when_two_advices_match_it(tmp_path):
    """Two payments for the same amount inside the same window, one deposit:
    the money landed once, so exactly one of them closes."""
    d = tmp_path / "d"
    twin = _message(
        id="msg-twin",
        subject=f"Remittance Advice - {ACCOUNT} Payment# 369784999",
        body=_remittance_html(payment_number="369784999"),
    )
    mailbox = _mailbox(tmp_path, _message(), twin)
    folder = _folder(tmp_path, ("09/15", AMOUNT, DEPOSIT_MEMO))

    result = _run(d, mailbox, folder=folder)

    assert result.status == "ok", result.summary
    assert len(_events(d, RECEIVED)) == 2
    assert len(_events(d, CLEARED)) == 1


def test_a_deposit_that_matches_nothing_clears_nothing(tmp_path):
    """Money in that is not this payment leaves the remittance open."""
    d = tmp_path / "d"
    folder = _folder(tmp_path, ("09/15", "12,000.00", DEPOSIT_MEMO))

    result = _run(d, _mailbox(tmp_path, _message()), folder=folder)

    assert result.status == "ok", result.summary
    assert len(_events(d, RECEIVED)) == 1
    assert _events(d, CLEARED) == []


def test_a_deposit_outside_the_clearing_window_clears_nothing(tmp_path):
    """The payment date is the anchor: a like-amount deposit from before the
    payment was even made is not this payment."""
    d = tmp_path / "d"
    folder = _folder(tmp_path, ("09/01", AMOUNT, DEPOSIT_MEMO))

    result = _run(d, _mailbox(tmp_path, _message()), folder=folder)

    assert result.status == "ok", result.summary
    assert _events(d, CLEARED) == []


# ---- what the morning brief reads --------------------------------------------


def test_the_run_writes_the_note_the_brief_reads(tmp_path):
    """05_Reports/_ar/remittance-<day>.md, in the section shape the brief's
    note reader already understands."""
    d = tmp_path / "d"
    reports = tmp_path / "reports"

    result = _run(d, _mailbox(tmp_path, _message()), reports=reports)

    assert result.status == "ok", result.summary
    notes = sorted((reports / "_ar").glob("remittance-*.md"))
    assert len(notes) == 1
    text = notes[0].read_text()
    assert "## Money in" in text
    assert PAYMENT in text
    assert "$58,250.00" in text


def test_shadow_records_nothing_and_writes_nothing(tmp_path):
    """Read-and-report: no event, no note, and the next live run still acts."""
    d = tmp_path / "d"
    reports = tmp_path / "reports"

    result = _run(d, _mailbox(tmp_path, _message()), reports=reports, shadow=True)

    assert result.status == "ok", result.summary
    assert _events(d, RECEIVED) == []
    assert not (reports / "_ar").exists()
    assert any(PAYMENT in action for action in result.actions)
