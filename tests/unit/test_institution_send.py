"""A missionary's receipts go to their sending organization on their yes
(Robb's idea 1, decided 2026-10-13: "a card with the missionary's yes,
maybe once a week").

For a ministry whose institution keeps its own books ([institution]
submit_to), the send job gathers every receipt not yet sent into one card
per person. The person's yes sends them; each claim carries what was paid
as they said it, its dollar value when the day's rate is known, and their
own words. When the institution is down, the claims are held, the person
hears it in plain words, and the next run sends them without asking again.
Nothing is ever sent twice, and Tim's own month report stays out of it, so
a receipt is never both claimed from the institution and paid by the books.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from core.adapters.pretend_institution import PretendInstitution
from core.agents.expenses.jobs import HELD_EVENT, REVIEW_CARD, SEND_CARD, SENT_EVENT
from core.contracts.institution import Claim as Sent
from core.engine.runner import run
from core.ledger import Ledger
from core.tools.receipts import Claim
from tests.unit.test_door_my_words import _says, solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import JPEG, _door, _snap
from tests.unit.test_missionary_month import NPR_OCT_12, feed  # noqa: F401  (fixture)

AGWM = '\n[institution]\nname = "AGWM"\ncategorizes = false\nsubmit_to = "pretend"\n'
BUS = "4,300 rupees for the bus to Pokhara"


def _agwm(solo) -> PretendInstitution:  # noqa: F811
    toml = solo["root"] / "living-water" / "tenant.toml"
    toml.write_text(toml.read_text() + AGWM)
    return PretendInstitution(Path(solo["ledger_root"]) / "institution")


def _job(solo, job: str = "send", **params: str):  # noqa: F811
    return run(
        "living-water",
        "expenses",
        job,
        params=params,
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )


def _send_cards(solo) -> list[dict]:  # noqa: F811
    with Ledger.open(solo["ledger_root"]) as ledger:
        return [r for r in ledger.list_approvals("living-water") if r["action_type"] == SEND_CARD]


def _events(solo, kind: str) -> list[dict]:  # noqa: F811
    with Ledger.open(solo["ledger_root"]) as ledger:
        return [e["payload"] for e in ledger.read_event_log() if e["event_type"] == kind]


def _yes(solo, card: dict) -> None:  # noqa: F811
    with Ledger.open(solo["ledger_root"]) as ledger:
        ledger.resolve_approval("living-water", int(card["id"]), "approved")


def test_a_ministry_that_keeps_its_own_books_sends_nothing(solo):  # noqa: F811
    _snap(solo, BUS, Claim(Decimal("4300"), "", "rupees"))
    done = _job(solo)
    assert done.status == "ok" and "no institution" in done.summary
    assert _send_cards(solo) == []


def test_the_receipts_wait_on_one_card_for_the_persons_yes(solo, feed):  # noqa: F811
    feed("2026-10-12", npr=NPR_OCT_12)
    agwm = _agwm(solo)
    _snap(solo, BUS, Claim(Decimal("4300"), "", "rupees"))
    _job(solo)
    [card] = _send_cards(solo)
    params = card["params"]
    assert card["status"] == "pending"
    assert params["person"] == "Daniel Reyes" and params["institution"] == "AGWM"
    assert params["count"] == "1"
    assert params["what"] == "1 receipt to send to AGWM"
    assert "2026-10-12: NPR 4,300.00" in params["lines"] and "Pokhara" in params["lines"]
    assert agwm.sent() == []  # nothing leaves before the yes


def test_the_yes_sends_each_claim_as_the_person_said_it(solo, feed):  # noqa: F811
    feed("2026-10-12", npr=NPR_OCT_12)
    agwm = _agwm(solo)
    _snap(solo, BUS, Claim(Decimal("4300"), "", "rupees"))
    _job(solo)
    _yes(solo, _send_cards(solo)[0])
    _job(solo)
    [claim] = agwm.sent()
    fields = {k: v for k, v in claim.items() if k in Sent.__dataclass_fields__}
    assert fields["person"] == "Daniel Reyes" and fields["day"] == "2026-10-12"
    assert (fields["amount"], fields["currency"]) == ("4300.00", "NPR")
    assert fields["usd"] == "28.06"  # 4,300 / 153.25665518, at the receipt's own day's rate
    assert fields["said"] == BUS
    assert fields["photo"].endswith(".jpg")
    [sent] = _events(solo, SENT_EVENT)
    assert sent["reference"] == "PRETEND-0001" and sent["receipt"] == claim["receipt"]
    _job(solo)  # the next morning: nothing goes twice, and no new card
    assert len(agwm.sent()) == 1 and len(_send_cards(solo)) == 1


def test_when_the_institution_is_down_the_claim_is_held_and_sent_later(solo):  # noqa: F811
    agwm = _agwm(solo)
    _snap(solo, "$43 for the clinic", Claim(Decimal("43"), "USD", "$"))
    _job(solo)
    _yes(solo, _send_cards(solo)[0])
    agwm.go_down()
    _job(solo)
    [held] = _events(solo, HELD_EVENT)
    assert held["say"] == (
        "AGWM isn't answering right now. "
        "Tim is holding this receipt and will send it when AGWM is back."
    )
    assert agwm.sent() == []
    agwm.come_back()
    _job(solo)  # no second yes: the person already said it
    [claim] = agwm.sent()
    assert claim["usd"] == "43.00" and claim["currency"] == "USD"
    assert len(_send_cards(solo)) == 1


def test_a_new_receipt_joins_the_waiting_card(solo):  # noqa: F811
    _agwm(solo)
    _snap(solo, "$43 for the clinic", Claim(Decimal("43"), "USD", "$"))
    _job(solo)
    _snap(solo, "$12 for printing", Claim(Decimal("12"), "USD", "$"), JPEG + b"2")
    _job(solo)
    cards = {c["status"]: c for c in _send_cards(solo)}
    assert set(cards) == {"pending", "superseded"}
    assert cards["pending"]["params"]["count"] == "2"


def test_the_persons_yes_is_their_own_tap_and_the_card_says_what_goes(solo):  # noqa: F811
    """A send leaves the building, so the yes is the person's own Face ID,
    like any card that moves no money; the page names what goes and where."""
    _agwm(solo)
    _snap(solo, "$43 for the clinic", Claim(Decimal("43"), "USD", "$"))
    _job(solo)
    key = _send_cards(solo)[0]["idempotency_key"]
    said = _says(solo, "daniel-reyes", key, "yes, send my receipts to AGWM")
    assert said["status"] == "tap_to_confirm"
    [asked] = solo["desk"].asked
    assert asked["person"] == "daniel-reyes"
    # each receipt rides under the headline, inside what the Face ID signs
    assert asked["summary"] == (
        "Approve 1 receipt to send to AGWM for Daniel Reyes.\nOct 12: $43.00, $43 for the clinic"
    )


def test_the_month_report_stays_out_of_an_institutions_receipts(solo):  # noqa: F811
    _agwm(solo)
    _snap(solo, "$43 for the clinic", Claim(Decimal("43"), "USD", "$"))
    done = _job(solo, "month", month="2026-10")
    assert "AGWM" in done.summary
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    reviews = con.execute(
        "SELECT COUNT(*) FROM approval_queue WHERE action_type = ? AND params_json LIKE ?",
        (REVIEW_CARD, '%"2026-10"%'),
    ).fetchone()[0]
    con.close()
    assert reviews == 0


def test_the_person_can_ask_what_has_gone_and_what_has_not(solo):  # noqa: F811
    agwm = _agwm(solo)
    _snap(solo, "$43 for the clinic", Claim(Decimal("43"), "USD", "$"))
    _job(solo)
    _yes(solo, _send_cards(solo)[0])
    _job(solo)
    _snap(solo, "$12 for printing", Claim(Decimal("12"), "USD", "$"), JPEG + b"2")
    _job(solo)
    assert len(agwm.sent()) == 1
    [person] = _door(solo, "daniel-reyes").call("month_report", {"month": "2026-10"})["people"]
    sent = sorted(line["sent"] for line in person["lines"])
    assert sent == ["Sent to AGWM", "Waiting for your yes"]
    assert person["review"] == "Goes to AGWM on your yes"
