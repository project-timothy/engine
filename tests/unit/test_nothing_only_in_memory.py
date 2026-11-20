"""Nothing only in memory (shot 4, card H3): a room naps after quiet
minutes and wakes on the next knock, so what a person holds in her hand
mid-flow, a receipt link, its question, a month page's buttons, must still
work after the nap. Kept beside the books, private to the room's account,
links by their hash only.

Held statements (a bank file waiting on her word) stay in memory on
purpose: nothing of a statement is written before she says whose it is."""

import json
import stat
from decimal import Decimal

from core.engine import private_file
from core.tools import month_page as mp
from core.tools.receipts import Claim, ReceiptDesk


def _desk(tmp_path, now):
    return ReceiptDesk(clock=lambda: now[0], state=tmp_path / "state" / "receipt-links.json")


def test_a_receipt_link_works_after_a_nap(tmp_path):
    now = [1000.0]
    token = _desk(tmp_path, now).issue(
        "hannah-cole", "Hannah Cole", Claim(Decimal("12.00"), "USD", "$"), "taxi"
    )
    ticket, why = _desk(tmp_path, now).take(token)  # a new process: the room woke
    assert why == "" and ticket.person == "hannah-cole" and ticket.claim.amount == Decimal("12.00")
    assert ticket.said == "taxi"


def test_a_spent_link_stays_spent_and_an_old_one_stays_old_after_a_nap(tmp_path):
    now = [1000.0]
    first = _desk(tmp_path, now)
    used = first.issue("hannah-cole", "Hannah Cole", None)
    old = first.issue("hannah-cole", "Hannah Cole", None)
    assert first.take(used)[1] == ""
    now[0] += 16 * 60
    woke = _desk(tmp_path, now)
    assert woke.gone(used) == "used" and woke.take(used) == (None, "used")
    assert woke.gone(old) == "expired"


def test_a_catch_up_link_keeps_its_day_across_a_nap(tmp_path):
    now = [1000.0]
    token = _desk(tmp_path, now).issue_pile("hannah-cole", "Hannah Cole", by="hannah-cole")
    now[0] += 3600
    woke = _desk(tmp_path, now)
    assert woke.is_pile(token) and woke.take(token)[1] == ""


def test_the_receipt_page_question_survives_a_nap(tmp_path):
    now = [1000.0]
    token = _desk(tmp_path, now).ask(
        "hannah-cole", "ab" * 32, {"a": ("USD12.00", "12.00")}, "Travel", no_day=True
    )
    asked, why = _desk(tmp_path, now).answer(token)
    assert why == "" and asked.choices == {"a": ("USD12.00", "12.00")} and asked.no_day


def test_the_file_is_private_and_holds_no_usable_link(tmp_path):
    now = [1000.0]
    desk = _desk(tmp_path, now)
    token = desk.issue("hannah-cole", "Hannah Cole", None)
    path = tmp_path / "state" / "receipt-links.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert token not in path.read_text()


def test_expired_links_leave_the_file(tmp_path):
    now = [1000.0]
    desk = _desk(tmp_path, now)
    desk.issue("hannah-cole", "Hannah Cole", None)
    now[0] += 2 * 24 * 3600
    desk.issue("hannah-cole", "Hannah Cole", None)
    kept = json.loads((tmp_path / "state" / "receipt-links.json").read_text())
    assert len(kept["open"]) == 1 and kept["used"] == {}


def test_a_desk_with_no_file_keeps_to_memory_as_before(tmp_path):
    desk = ReceiptDesk()
    token = desk.issue("hannah-cole", "Hannah Cole", None)
    assert desk.take(token)[1] == ""
    assert not list(tmp_path.iterdir())


def test_the_month_page_buttons_survive_a_nap(tmp_path):
    key = tmp_path / "state" / "month-page.key"
    one = mp.MonthPage(desk=None, tools_for=lambda p: None, key_file=key)
    two = mp.MonthPage(desk=None, tools_for=lambda p: None, key_file=key)
    assert one._key == two._key and len(one._key) == 32
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert mp.MonthPage(desk=None, tools_for=lambda p: None)._key != one._key


def test_one_private_writer_for_the_talk_store_and_the_desk(tmp_path):
    from core.talk import store

    assert store._keep is private_file.keep and store._name is private_file.plain_name
    assert (
        private_file.state_folder(tmp_path / "ledger", "hope-springs")
        == tmp_path / "state" / "hope-springs"
    )
