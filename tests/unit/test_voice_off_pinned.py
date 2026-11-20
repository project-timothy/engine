"""Where Tim's own voice is off, setup, the talk page, start_here, and the
FAQ stay byte for byte (Standalone Tim shot 3, card T2, 2026-11-17).

Card T2 turns setup toward Tim's own page, but only for a ministry whose
``tenant.toml`` names a tier for ``talk``. Every real box has it off tonight,
and a live alpha box's owner uses the connector door, so for a ministry with
the voice off every screen, word, page, and answer is pinned here by hash,
taken from main before the card changed anything. A change to one of them is
a deliberate act: change it, then the pin, in the same PR, and say why.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from core.capabilities import DOOR_SPECS
from core.capabilities.faq import faq_for, load_faq, markdown
from core.onboarding.person import Facts, load_steps, offered, words
from core.tools import talk_page as tp
from core.tools.receipts import ReceiptDesk, Receipts
from core.tools.setup_page import NEXT_WORDS
from core.tools.start import _start_here
from core.tools.viewer import viewer_for
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_setup_connector_and_calendar import setup  # noqa: F401  (fixture)
from tests.unit.test_setup_page import _call, _yes
from tests.unit.test_setup_page import setup as _setup  # noqa: F401  (fixture)

PINNED = {
    "steps": "0e194c5ce12aae16f77e0e3178798deebbbda4b482930ba84ddfc682d25737d6",
    "words": "feecc4642c2a1419a0d8c7c49928f7032bef3925171b0467d034869731bd792f",
    "offered": "15e9e15f34dbab26d1519de2d5d91abee372afee79d8c18fec60688c03398e9f",
    "next_words": "6aa6357f9bd19a938b7064bf0835772fd222ade8395ab2f2c8d390155f04f932",
    "screen_ai": "64ce87599e0da39a30187d7132837cb9822122c657fcbf658480e3281c616353",
    "screen_home_ask": "c7e808e5674ff642d5b9bd6cda48b948be77324de6ad95e0e28030d565b515bc",
    "screen_home_check": "150607c27fc277fa7b91707043fb06763f36b4259c5c1bd5fb0325fba9eb8b57",
    "screen_done": "4d6f66d39a2b2c9d13dc7cc204844d55a802f2be5409ec3e055c75d4558a0870",
    "talk_manifest": "dcd8d3a425d8e96b6aaf872d67cffb550bcfb8f7bacc5683e49814fa4aeda757",
    # Plain words (the owner's read of 2026-11-19): the sign-in page says
    # "this device" and names Touch ID and Windows Hello for a computer;
    # the talk page says how to keep Tim on a computer, and its closed
    # button says why it closed.
    # A pile of receipts (the owner's read of 2026-11-19): the talk page's
    # photo button gets "Send several at once" beside it, and the style the
    # sign-in page shares lays them out.
    "talk_sign_in": "d7dbcbe8d8254a2812a6053741e3c50dbd00d77c2bfa51a36f42c9040d0e4efb",
    "talk_page": "b3fdc7253706bd5c5144a9d1b8b58e3d3f8bd1419e6e96347e46bc97d0bd7949",
    # Card R2 (2026-11-18) added two answers, letter-read-at-setup (for
    # everyone) and statement-photo (bank_statement, on every door), which
    # start_here carries: the only change (checked against main's FAQ file).
    # The walkthrough items of 2026-11-18 added statement-names (bank_statement),
    # keep-flagged (keep_own_lines), and csv-list-view (catch_up_link), all on
    # every door, which start_here carries: the only change.
    # t-payer-split (2026-11-18) added some-of-each (keep_payers, tax_packet),
    # which start_here carries: the only change (checked against main's FAQ file).
    # The typed yes (2026-11-18) added talk-typed-yes (decide_card) and said in
    # talk-yes that an approval over the line opens the yes page: the FAQ file is
    # every ministry's, so it changes here too; nothing else a voice-off ministry
    # sees moved (start_here, setup, and the talk page stay as pinned).
    # The 1099 card's words (V4 Y02, 2026-11-18) added form-differs
    # (keep_payers, tax_packet), which start_here carries: the only change.
    # A held call no longer ends Tim's turn (V4 run 3, 2026-11-18) added
    # talk-yes-and-answer (decide_card): the FAQ file is every ministry's;
    # start_here, setup, and the talk page stay as pinned.
    # Tim hears what her tap did (V4 run 3, 2026-11-18) added talk-tap-heard
    # (add_receipt, decide_card), which start_here carries: the only change.
    # Whose yes a card is (the Haiku run, A09, 2026-11-18) added whose-yes
    # (waiting_cards, decide_card), which start_here carries: the only change.
    # Her yes button back once (the owner's call of 2026-11-18, A01) added
    # talk-yes-back (decide_card) and a line in talk-tap-heard: the FAQ file
    # and its page move; start_here, setup, and the talk page stay as pinned.
    # Tim holds what he noticed (the owner's call of 2026-11-18, D06) added
    # talk-answers-what-you-asked (month_report, waiting_cards), which
    # start_here carries: the only change.
    # Taking back a yes (the owner's call of 2026-11-18, A06) added take-back
    # (decide_card): the FAQ file and its page move; start_here, setup, and
    # the talk page stay as pinned.
    # Plain words (the owner's read of 2026-11-19): "card" leaves nine
    # answers (it read as a credit card), take-back says when it stands in
    # her terms, and "Already pay for Claude or ChatGPT?": the FAQ file and
    # its page move; start_here and setup stay as pinned.
    # A person's write waits a moment for the books (the owner's read of
    # 2026-11-19) added books-busy, an answer for everyone, so start_here,
    # which carries those, moves with the FAQ file and its page.
    # talk-to-tim names "Send several at once" (a pile of receipts, 2026-11-19).
    # The ticket's day (the owner's call of 2026-11-19, "ask on the ticket")
    # added ticket-day (add_receipt), which start_here carries for anyone who
    # files receipts: the FAQ file, its page, and start_here move.
    # "It's approved." (T2T, 2026-11-19): talk-typed-yes says a plain approval
    # is enough when one thing waits on her yes.
    # No card numbers in Tim's words (the Haiku run, A09, 2026-11-18) added
    # talk-no-numbers (waiting_cards, decide_card), which start_here carries:
    # the only change.
    # A link that outlives a nap (shot 4, card H3, 2026-11-19) added
    # link-after-quiet (add_receipt), which start_here carries: the only change.
    # faq, faq_page: an example amount changed for the public export 2026-11-20
    "faq": "5311bf1f9da0e9a4f5a1f48bcc873edb6867dc81eca74ffa48e33f1e8fe10769",
    "faq_page": "a871dfc9ec9fcc4cbe29b3d5afadd36ba6071e55f373e57b01f17ea765626236",
    "start_here": "c34ce84e04540b9c93724b8e738c86d9855f50937eb590d6341495b105f14f23",
}


def _hash(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _setup_screens(setup) -> dict[str, object]:  # noqa: F811
    """Every setup screen a voice-off ministry shows, with the one random
    part (the receipt page's own key) cut out of the link."""
    got: dict[str, object] = {}
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "daily_email"})
    got["screen_ai"] = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "ai_connector"})
    got["screen_home_ask"] = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
    home = dict(_yes(setup, "daniel-reyes", "home_screen")[1])
    home["open"] = home["open"].rsplit("/", 1)[0]
    got["screen_home_check"] = home
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "home_screen"})
    got["screen_done"] = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
    return got


def _talk(solo) -> tp.TalkPage:  # noqa: F811
    return tp.TalkPage(
        "living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        desk=type("D", (), {"session_person": lambda self, token: None})(),
        origin="https://tim.example.org",
        tools_for=lambda person: None,
        receipts=ReceiptDesk(),
        ministry="Living Water Mozambique, Inc.",
    )


def _start(solo) -> dict:  # noqa: F811
    from core.tools import catalog

    tools = catalog.Tools(
        "living-water",
        ledger_root=solo["ledger_root"],
        viewer=viewer_for("living-water", "daniel-reyes", tenants_root=solo["root"]),
        ministry="Living Water Mozambique, Inc.",
        today="2026-11-17",
        receipts=Receipts(
            "living-water",
            tenants_root=solo["root"],
            ledger_root=solo["ledger_root"],
            person="daniel-reyes",
            desk=ReceiptDesk(),
            base_url="https://tim.example.org/receipt",
        ),
    )
    return _start_here(tools, {})


@pytest.fixture
def served(setup, solo):  # noqa: F811
    """What a voice-off ministry serves, by name."""
    got: dict[str, object] = {
        "steps": [s.__dict__ for s in load_steps()],
        "words": {s.id: words(s.id, email="e@example.org", who="Daniel") for s in load_steps()},
        "offered": [
            offered(Facts(email=e, files_expenses=f, adopts_plan=a), offer)
            for e in ("", "e@example.org")
            for f in (False, True)
            for a in (False, True)
            for offer in ((), ("ai_connector",), ("mileage", "calendar", "ai_connector"))
        ],
        "next_words": NEXT_WORDS,
        **_setup_screens(setup),
    }
    page = _talk(solo)
    got["talk_manifest"] = page.answer("GET", tp.MANIFEST_PATH, {}, b"")[2].decode()
    got["talk_sign_in"] = tp.sign_in_page("n0nce", ministry="Living Water Mozambique, Inc.")
    got["talk_page"] = tp.render(
        [{"said": "Hey Tim", "tim": "Hi Daniel."}],
        nonce="n0nce",
        ministry="Living Water Mozambique, Inc.",
        nxt={"say": "Send your first bank statement.", "takes": "about 2 minutes"},
        photo=True,
    )
    got["faq"] = faq_for(s.name for s in DOOR_SPECS)
    got["faq_page"] = markdown(load_faq())
    got["start_here"] = _start(solo)
    return {name: _hash(value) for name, value in got.items()}


def test_every_part_a_voice_off_ministry_serves_is_pinned(served):
    assert sorted(served) == sorted(PINNED)


def test_a_voice_off_ministry_is_served_byte_for_byte_as_pinned(served):
    changed = [name for name, digest in served.items() if PINNED[name] != digest]
    assert changed == [], f"a voice-off ministry now sees something new in: {changed}"
