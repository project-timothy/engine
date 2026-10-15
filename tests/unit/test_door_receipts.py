"""Receipts in from the person's own phone (the owner's design of 2026-10-12).

"Hey Tim, here's a $43 receipt": the person's AI calls add_receipt with
their own words, and code, never the AI, reads the amount and currency from
them. Tim answers with a one-use link that opens the phone's camera on the
box's own page; the photo lands in the person's own folder of the expense
drop tree, the claim in its name, and intake files it at once. Adding a
receipt commits no money (the month's report is the yes), so the link
needs no Face ID. A currency other than dollars is kept as said; its dollar
value is set with the monthly report."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

from core.tools import catalog
from core.tools.receipts import Claim, ReceiptDesk, Receipts, claim_in
from core.tools.viewer import viewer_for
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
BASE = "https://tim.example.org/receipt"


# ---- the claim comes from the person's own words ------------------------------------


@pytest.mark.parametrize(
    ("said", "amount", "currency", "unit"),
    [
        ("here's a $43 receipt", "43.00", "USD", "$"),
        ("Hey Tim, here's a $43.20 receipt from Shoprite on Oct 3", "43.20", "USD", "$"),
        ("43 dollars for gas", "43.00", "USD", "dollars"),
        ("lunch was 12 bucks", "12.00", "USD", "bucks"),
        ("4,300 rupees at the market", "4300.00", "", "rupees"),
        ("NPR 4300 for the bus", "4300.00", "NPR", "NPR"),
        ("1,250 MZN fuel", "1250.00", "MZN", "MZN"),
        ("€12.50 for the train", "12.50", "EUR", "€"),
    ],
)
def test_the_amount_and_currency_come_from_the_persons_words(said, amount, currency, unit):
    assert claim_in(said) == Claim(Decimal(amount), currency, unit)


@pytest.mark.parametrize("said", ["here's a receipt", "two receipts from Oct 3", "", "receipt 43"])
def test_words_without_a_marked_amount_make_no_claim(said):
    assert claim_in(said) is None


def test_a_claim_reads_and_files_the_way_it_was_said():
    assert Claim(Decimal("43.2"), "USD", "$").words() == "$43.20"
    assert Claim(Decimal("4300"), "", "rupees").words() == "4,300.00 rupees"
    assert Claim(Decimal("43.2"), "USD", "$").tag() == "$43.20"  # intake's amount tag
    assert Claim(Decimal("4300"), "", "rupees").tag() == "4300.00-rupees"  # never a dollar tag
    assert Claim(Decimal("4300"), "NPR", "NPR").tag() == "NPR4300.00"


# ---- the tool -----------------------------------------------------------------------


def _tools(solo, person: str, desk: ReceiptDesk) -> catalog.Tools:  # noqa: F811
    return catalog.Tools(
        "living-water",
        ledger_root=solo["ledger_root"],
        viewer=viewer_for("living-water", person, tenants_root=solo["root"]),
        receipts=Receipts(
            "living-water",
            tenants_root=solo["root"],
            ledger_root=solo["ledger_root"],
            person=person,
            desk=desk,
            base_url=BASE,
        ),
    )


def test_daniel_is_offered_add_receipt_and_linda_is_not(solo):  # noqa: F811
    desk = ReceiptDesk()
    assert "add_receipt" in {s.name for s in _tools(solo, "daniel-reyes", desk).specs()}
    assert "add_receipt" not in {s.name for s in _tools(solo, "linda-park", desk).specs()}


def test_his_words_get_a_one_tap_camera_link(solo):  # noqa: F811
    desk = ReceiptDesk()
    said = _tools(solo, "daniel-reyes", desk).call(
        "add_receipt", {"said": "here's a $43 receipt from Shoprite"}
    )
    assert said["status"] == "tap_to_add"
    assert said["show"].startswith(f"[Tap to add your receipt]({BASE}#")
    assert "Face ID" not in said["say"]


def test_with_no_amount_the_link_comes_and_tim_asks_after(solo):  # noqa: F811
    """Retargeted 2026-10-13 (t-receipt-photo): no amount used to mean no
    link; now the photo comes first and the amount after, from their words."""
    desk = ReceiptDesk()
    said = _tools(solo, "daniel-reyes", desk).call("add_receipt", {"said": "here's a receipt"})
    assert said["status"] == "tap_to_add" and "show" in said
    assert "how much" in said["say"].lower() and "Never guess an amount" in said["say"]


# ---- the link: one use, fifteen minutes ---------------------------------------------


def test_a_link_works_once_and_only_while_it_is_fresh():
    now = [1000.0]
    desk = ReceiptDesk(clock=lambda: now[0])
    token = desk.issue("daniel-reyes", "Daniel Reyes", Claim(Decimal("43"), "USD", "$"))
    ticket, why = desk.take(token)
    assert ticket is not None and ticket.person == "daniel-reyes" and why == ""
    assert desk.take(token) == (None, "used")
    late = desk.issue("daniel-reyes", "Daniel Reyes", Claim(Decimal("43"), "USD", "$"))
    now[0] += 15 * 60 + 1
    assert desk.take(late) == (None, "expired")
    assert desk.take("never-issued") == (None, "unknown")


# ---- the page and the upload, over the box's HTTP door ------------------------------


@pytest.fixture
def box(solo, tmp_path):  # noqa: F811
    from core.tools.mcp_http import TokenFile, make_server

    desk = ReceiptDesk()
    server = make_server(
        "living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        obligations_file=None,
        verifier=TokenFile(tmp_path / "tokens.json"),
        resource="https://tim.example.org/mcp",
        authorization_servers=["https://signin.example.org"],
        port=0,
        receipts=desk,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {**solo, "base": f"http://127.0.0.1:{server.server_address[1]}", "desk": desk}
    finally:
        server.shutdown()


def _post(box, token: str, data: bytes, kind: str = "image/jpeg") -> tuple[int, dict]:
    req = urllib.request.Request(
        box["base"] + "/receipt",
        data=data,
        method="POST",
        headers={"Content-Type": kind, "X-Receipt-Ticket": token},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _filed(box) -> list[Path]:
    root = Path.cwd()
    return [p for p in root.rglob("*") if p.is_file() and "/receipts/Daniel Reyes/" in str(p)]


def test_the_page_opens_the_camera(box):
    with urllib.request.urlopen(box["base"] + "/receipt") as resp:
        page = resp.read().decode()
    assert 'capture="environment"' in page and 'accept="image/*' in page
    assert "Content-Security-Policy" in resp.headers


def test_the_photo_lands_in_his_own_folder_and_is_filed_at_once(box):
    token = box["desk"].issue("daniel-reyes", "Daniel Reyes", Claim(Decimal("43"), "USD", "$"))
    status, said = _post(box, token, JPEG)
    assert status == 200, said
    assert said["say"].startswith("Got it: $43.00.")
    filed = _filed(box)
    assert len(filed) == 1 and filed[0].name.endswith("_$43.00.jpg")
    assert filed[0].parent.name == "General"  # the ministry's one fund: no "which project?"


def test_rupees_are_kept_as_said_and_wait_for_their_dollar_value(box):
    claim = Claim(Decimal("4300"), "", "rupees")
    token = box["desk"].issue("daniel-reyes", "Daniel Reyes", claim)
    status, said = _post(box, token, JPEG)
    assert status == 200
    assert said["say"].startswith("Got it: 4,300.00 rupees.")
    assert "dollar" in said["say"]
    assert _filed(box)[0].name.endswith("_4300.00-rupees.jpg")


def test_a_used_link_says_to_ask_tim_again(box):
    token = box["desk"].issue("daniel-reyes", "Daniel Reyes", Claim(Decimal("43"), "USD", "$"))
    assert _post(box, token, JPEG)[0] == 200
    status, said = _post(box, token, JPEG)
    assert status == 410 and "Ask Tim" in said["say"]
    assert len(_filed(box)) == 1


def test_only_a_photo_or_a_pdf_is_taken(box):
    token = box["desk"].issue("daniel-reyes", "Daniel Reyes", Claim(Decimal("43"), "USD", "$"))
    status, _ = _post(box, token, b"#!/bin/sh\necho hi\n", "image/jpeg")
    assert status == 415 and _filed(box) == []


def test_a_photo_too_large_is_refused(box):
    token = box["desk"].issue("daniel-reyes", "Daniel Reyes", Claim(Decimal("43"), "USD", "$"))
    try:
        status, _ = _post(box, token, JPEG + b"\x00" * (10 * 1024 * 1024))
    except urllib.error.URLError:  # refused before the body was read: the pipe closes
        status = 413
    assert status == 413 and _filed(box) == []
    assert _post(box, token, JPEG)[0] == 200  # a refused photo never cost him his link


def test_the_door_tells_the_ai_the_amount_is_never_its_own():
    from core.tools.mcp_http import DOOR_INSTRUCTIONS

    assert "add_receipt" in DOOR_INSTRUCTIONS
    assert "never yours" in DOOR_INSTRUCTIONS


def test_intake_runs_in_the_doors_own_ledger(box):
    """Found on the practice box 10/12: intake ran against <ledger>/<tenant>/<tenant>,
    a second ledger nobody reads, because the door's ledger root went in as
    the runner's base."""
    import sqlite3

    token = box["desk"].issue("daniel-reyes", "Daniel Reyes", Claim(Decimal("43"), "USD", "$"))
    assert _post(box, token, JPEG)[0] == 200
    ledger_root = Path(box["ledger_root"])
    assert not (ledger_root / "living-water").exists()
    con = sqlite3.connect(ledger_root / "ledger.sqlite3")
    jobs = con.execute("SELECT agent, job FROM runs WHERE agent = 'expenses'").fetchall()
    con.close()
    assert ("expenses", "intake") in jobs


def test_a_hidden_button_stays_hidden():
    # The first phone photos from CI (10/14): `button{display:block}` beat the
    # browser's own [hidden] rule, so the catch-up page showed the not-yet-
    # needed "Send these" button as an empty bar. The page's style restores it.
    from core.tools.receipts import page

    html = page("n", many=True)
    assert '<button id="send" type=button hidden>' in html
    assert "[hidden]{display:none!important}" in html
