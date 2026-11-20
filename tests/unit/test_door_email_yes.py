"""The emailed yes (the owner's decision of 2026-10-30: email and the phone's
number match are enough; a passkey is optional).

Carol has no passkey. She tells her AI to approve Ruth's report; the engine
asks the doorkeeper as always, with ``email=yes``, and the doorkeeper mails
the link to the address Grace gave her and says only that it went. The AI
hears "Tim emailed Carol a link" and never holds it. Her tap on the emailed
link finishes it the same way Face ID does; the card says which way it came
(``witness_kind``), and the auditor stands behind both.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from auditor.lenses import AuditContext
from auditor.lenses.approvals import decision_challenge, door_refusal
from core.tools.decide import AskBook
from core.tools.witness_desk import Asked, DeskRefused, WitnessDesk
from tests.unit.test_door_decide import (
    FakeDesk,
    _all,
    _cards,
    _carol,
    _decide,
    _finish,
    _new_vendor_card,
    _open_door,
    _ruths,
    _tools,
    _vouch,
    door,  # noqa: F401  (fixture)
)
from tests.unit.test_tools_answer_as_a_person import church  # noqa: F401  (fixture)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _email_evidence(tapper: str, card: int, summary: str, verb: str = "approve") -> dict:
    nonce = b"n" * 32
    signed = decision_challenge(nonce, tapper, str(card), verb, summary)
    return {"kind": "email", "nonce": _b64u(nonce), "challenge": _b64u(signed), "seal": "s"}


# ---- what the AI hears


def test_with_no_passkey_the_ai_hears_tim_emailed_her_and_holds_no_link(door):  # noqa: F811
    door["desk"].emailed = {"carol-jennings"}
    said = _decide(_carol(door), _ruths(door["church"])["id"])
    assert said["status"] == "emailed"
    assert "link" not in said and "show" not in said
    assert "https://" not in json.dumps(said)
    assert said["say"].startswith("Tim emailed Carol Jennings a link to approve it")
    assert "no link for you to show" in said["say"]


def test_asking_again_before_the_tap_hears_it_is_in_her_email(door):  # noqa: F811
    door["desk"].emailed = {"carol-jennings"}
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    again = _decide(_carol(door), card)
    assert again["status"] == "emailed" and "already emailed" in again["say"]
    assert len(door["desk"].asked) == 1  # one email, not two


def test_a_lapsed_emailed_link_is_replaced_and_said_kindly(door):  # noqa: F811
    door["desk"].emailed = {"carol-jennings"}
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    door["desk"].lapse()
    fresh = _decide(_carol(door), card)
    assert fresh["say"].startswith("Their last link timed out") and len(door["desk"].asked) == 2


def test_too_much_mail_this_hour_is_said_plainly(door):  # noqa: F811
    door["desk"].refuse = "too_many"
    try:
        _decide(_carol(door), _ruths(door["church"])["id"])
    except ValueError as exc:
        assert "emailed you a lot this hour" in str(exc)
    else:
        raise AssertionError("refused")


def test_several_cards_on_one_emailed_link(door):  # noqa: F811
    door["desk"].emailed = {"carol-jennings"}
    cards = _cards(door["church"])
    both = [cards["Ruth Hollis"]["id"], cards["Tom and Jen Baker"]["id"]]
    said = _carol(door).call(
        "decide_card", {"cards": both, "decision": "approve", "said": "approve them all"}
    )
    assert said["status"] == "emailed" and said["cards"] == both
    assert "link" not in said and "a link to approve all 2" in said["say"]


# ---- the tap finishes it, and the card says how


def test_her_tap_on_the_emailed_link_approves_it_and_the_card_says_email(door):  # noqa: F811
    church, desk = door["church"], door["desk"]  # noqa: F811
    desk.emailed = {"carol-jennings"}
    card = _ruths(church)["id"]
    _decide(_carol(door), card)
    asked = desk.asked[-1]
    desk.tap(evidence=_email_evidence("carol-jennings", card, asked["summary"]))
    _finish(church, desk)
    assert desk.settled[-1][1] == "approved"
    params = _ruths(church)["params"]
    assert params["decided_via"] == "door" and params["witness_kind"] == "email"


def test_a_face_id_yes_says_passkey(door):  # noqa: F811
    church, desk = door["church"], door["desk"]  # noqa: F811
    _decide(_carol(door), _ruths(church)["id"])
    desk.tap()
    _finish(church, desk)
    assert _ruths(church)["params"]["witness_kind"] == "passkey"


# ---- the auditor stands behind it


def _ctx(church) -> AuditContext:  # noqa: F811
    return AuditContext(
        tenant=SimpleNamespace(slug="grace"),
        ledger=None,
        now=datetime.now(UTC),
        store_root=church["root"],
        tenants_dir=church["root"],
    )


def _human_only_by_email(church, evidence_for=None):  # noqa: F811
    _open_door(church, "vendor")
    card, desk = _new_vendor_card(church), FakeDesk()
    desk.emailed = {"carol-jennings"}
    _decide(_tools(church, "carol-jennings", desk, AskBook()), card)
    asked = desk.asked[-1]
    evidence = (evidence_for or _email_evidence)("carol-jennings", card, asked["summary"])
    desk.tap(evidence=evidence)
    _finish(church, desk)
    row = next(r for r in _all(church) if r["id"] == card)
    return card, row


def test_the_auditor_stands_behind_an_emailed_yes(church):  # noqa: F811
    card, row = _human_only_by_email(church)
    assert row["status"] == "approved" and row["params"]["witness_kind"] == "email"
    assert door_refusal(_ctx(church), {"id": card, "status": "approved"}, row["params"]) == ""


def test_an_emailed_yes_for_another_decision_is_named(church):  # noqa: F811
    def wrong(tapper, card, summary):
        return _email_evidence(tapper, card + 1, summary)

    card, row = _human_only_by_email(church, wrong)
    refused = door_refusal(_ctx(church), {"id": card, "status": "approved"}, row["params"])
    assert refused == "the emailed yes is not carol-jennings's approve on this card"


def test_an_emailed_yes_with_no_seal_cannot_be_read(church):  # noqa: F811
    def unsealed(tapper, card, summary):
        return {k: v for k, v in _email_evidence(tapper, card, summary).items() if k != "seal"}

    card, row = _human_only_by_email(church, unsealed)
    refused = door_refusal(_ctx(church), {"id": card, "status": "approved"}, row["params"])
    assert refused == "the signed evidence cannot be read"


def test_a_card_whose_kind_and_evidence_disagree_is_named(church):  # noqa: F811
    card, row = _human_only_by_email(church)
    params = {**row["params"], "witness_kind": "passkey"}
    refused = door_refusal(_ctx(church), {"id": card, "status": "approved"}, params)
    assert refused == "the card says passkey, but the evidence is email"


def test_a_vouched_yes_by_email_goes_to_the_voucher(church):  # noqa: F811
    _open_door(church, "vendor")
    _vouch(church, 'carol-jennings = "don-pruitt"')
    card, desk = _new_vendor_card(church), FakeDesk()
    desk.no_passkey, desk.emailed = {"carol-jennings"}, {"don-pruitt"}
    said = _decide(_tools(church, "carol-jennings", desk, AskBook()), card)
    assert said["status"] == "emailed" and said["vouched_by"] == "Don Pruitt"
    assert "link" not in said and "Don Pruitt confirms for them" in said["say"]


# ---- the desk asks with email=yes, and hears it went


def test_the_desk_asks_with_email_and_reads_an_emailed_reply(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("x" * 40)
    desk = WitnessDesk(url="http://127.0.0.1:1", secret_file=secret)
    sent: list[dict] = []

    def post(path, form, timeout=None):
        sent.append(form)
        return 200, {"witness": "w" * 32, "emailed": True, "expires_in": 1800}

    desk._post = post  # type: ignore[method-assign]
    asked = desk.ask("carol-jennings", "214", "approve", "Approve Ruth's report.")
    assert asked == Asked("w" * 32, "", emailed=True)
    assert sent[0]["email"] == "yes"


def test_an_emailed_reply_with_no_witness_is_no_yes(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("x" * 40)
    desk = WitnessDesk(url="http://127.0.0.1:1", secret_file=secret)
    desk._post = lambda path, form, timeout=None: (200, {"emailed": True})  # type: ignore[method-assign]
    try:
        desk.ask("carol-jennings", "214", "approve", "x")
    except DeskRefused as exc:
        assert exc.reason == "unreachable"
    else:
        raise AssertionError("refused")
