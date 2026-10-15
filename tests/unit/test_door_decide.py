"""Deciding a card through the box's door, with the person's own Face ID
(engine #465, step 2b; the owner's decision of 2026-10-09: "approve it" in
the chat or an Approve button, and Face ID proves it was the person).

Carol, Grace Fellowship's treasurer, tells her AI to approve Ruth's report.
The AI calls ``decide_card``. The engine first checks Carol may decide it at
all, so her phone is never asked for something she cannot approve; then it
asks the doorkeeper for a one-time link and hands it back. Carol opens it,
reads the card, taps Approve, Face ID, and the tap finishes it (engine #480,
the owner's design day of 2026-10-11): the engine's finisher, already
waiting on the doorkeeper, hears her yes the moment it lands and decides the
card through the same path the terminal uses, stamped as decided by the door
with the signed evidence. Her Approve page says how it went; she never goes
back to the AI to finish.

The tool is offered only on a tenant whose authority.toml opened the door
(``[presence] door``), and only on the HTTP door with a doorkeeper.
"""

from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace

import pytest

from core.agents.ap.jobs import NEW_VENDOR_CARD
from core.engine.authority_gate import Witnessed
from core.engine.runner import ledger_write_lock
from core.ledger import Ledger
from core.tools import catalog
from core.tools.decide import AskBook, Decider, Finisher, finisher_for
from core.tools.viewer import viewer_for
from core.tools.witness_desk import Asked, DeskRefused, Tapped
from tests.unit.test_tools_answer_as_a_person import church  # noqa: F401  (fixture)

EVIDENCE = {"nonce": "n", "credential_id": "c", "signature": "s"}


class FakeDesk:
    """The doorkeeper, standing in: it hands out links, and when the person
    taps (``tap``) holds their yes for the engine's finisher until it says
    how it went."""

    def __init__(self) -> None:
        self.asked: list[dict] = []
        self.refuse = ""
        self.no_passkey: set[str] = set()
        self.stands: dict[str, str] = {}  # witness id -> waiting | witnessed | settled | unknown
        self.tapped: dict[int, tuple[str, Tapped]] = {}  # yes -> (witness id, the yes)
        self.settled: list[tuple[int, str, str]] = []

    def ask(
        self, person: str, card: str, verb: str, summary: str, for_person: str | None = None
    ) -> Asked:
        if self.refuse:
            raise DeskRefused(self.refuse)
        if person in self.no_passkey:
            raise DeskRefused("no_passkey")
        n = len(self.asked) + 1
        witness = f"witness-{n}"
        self.asked.append(
            {
                "person": person,
                "card": card,
                "verb": verb,
                "summary": summary,
                "for": for_person or person,
                "witness": witness,
            }
        )
        self.stands[witness] = "waiting"
        return Asked(witness, f"https://tim.example.org/yes#link-{n}")

    def check(self, witness: str) -> str:
        return self.stands.get(witness, "unknown")

    def tap(self, evidence: dict | None = None, **over) -> int:
        """The last link is tapped, with Face ID."""
        last = {**self.asked[-1], **over}
        yes = len(self.tapped) + 1
        witnessed = Witnessed(
            last["person"],
            last["card"],
            last["verb"],
            last["summary"],
            1_800_000_000.0,
            evidence or EVIDENCE,
        )
        self.tapped[yes] = (self.asked[-1]["witness"], Tapped(yes, last["for"], witnessed))
        self.stands[self.asked[-1]["witness"]] = "witnessed"
        return yes

    def lapse(self) -> None:
        self.stands[self.asked[-1]["witness"]] = "unknown"

    def next(self, skip, wait: float) -> list[Tapped]:
        waiting = [t for w, t in self.tapped.values() if self.stands[w] == "witnessed"]
        return [t for t in waiting if t.yes not in skip] or waiting

    def settle(self, yes: int, status: str, message: str) -> bool:
        witness, _ = self.tapped[yes]
        if self.stands[witness] != "witnessed":
            return False
        self.stands[witness] = "settled"
        self.settled.append((yes, status, message))
        return True


def _open_door(church, *resources: str) -> None:  # noqa: F811
    path = church["root"] / "grace" / "authority.toml"
    listed = ", ".join(f'"{r}"' for r in resources)
    path.write_text(path.read_text() + f"\n[presence]\ndoor = [{listed}]\n")
    _word_line(church, "0")  # these tests prove the tap; the dollar line has its own


def _word_line(church, amount: str) -> None:  # noqa: F811
    """Set Grace's dollar line: a yes said to the person's own AI decides a
    card up to this much (``[safeguards] yes_by_word_up_to``)."""
    path = church["root"] / "grace" / "authority.toml"
    text, n = re.subn(
        r"(?m)^yes_by_word_up_to = .*$", f"yes_by_word_up_to = {amount}", path.read_text()
    )
    assert n == 1, "onboarding writes the line"
    path.write_text(text)


def _tools(
    church,  # noqa: F811
    person: str,
    desk: FakeDesk,
    asks: AskBook | None = None,
) -> catalog.Tools:
    return catalog.Tools(
        "grace",
        ledger_root=church["ledger_root"],
        obligations_file=church["obligations"],
        viewer=viewer_for("grace", person, tenants_root=church["root"]),
        decider=Decider(
            "grace",
            tenants_root=church["root"],
            ledger_root=church["ledger_root"],
            person=person,
            desk=desk,
            asks=asks if asks is not None else AskBook(),
        ),
    )


def _cards(church) -> dict[str, dict]:  # noqa: F811
    with Ledger.open(church["ledger_root"]) as ledger:
        return {
            r["params"].get("person", r["action_type"]): r for r in ledger.list_approvals("grace")
        }


def _ruths(church) -> dict:  # noqa: F811
    return _cards(church)["Ruth Hollis"]


@pytest.fixture
def door(church):  # noqa: F811
    _open_door(church, "expense.report")
    desk, asks = FakeDesk(), AskBook()
    return {"church": church, "desk": desk, "asks": asks}


def _carol(door) -> catalog.Tools:
    return _tools(door["church"], "carol-jennings", door["desk"], door["asks"])


def _decide(tools: catalog.Tools, card: int, decision: str = "approve", said: str = "") -> dict:
    return tools.call("decide_card", {"card": card, "decision": decision, "said": said})


def _finisher(church, desk: FakeDesk) -> Finisher:  # noqa: F811
    return Finisher(
        "grace", tenants_root=church["root"], ledger_root=church["ledger_root"], desk=desk
    )


def _finish(church, desk: FakeDesk, held: frozenset[int] = frozenset()) -> set[int]:  # noqa: F811
    """One turn of the engine's finisher: every tapped yes written and settled."""
    return _finisher(church, desk).run_once(held)


# ---- where the tool is offered ----------------------------------------------------


def test_a_tenant_that_did_not_open_the_door_is_not_offered_it(church):  # noqa: F811
    tools = _tools(church, "carol-jennings", FakeDesk())
    assert "decide_card" not in [s.name for s in tools.specs()]


def test_with_the_door_open_the_tool_is_offered_and_says_it_writes(door):
    (spec,) = [s for s in _carol(door).specs() if s.name == "decide_card"]
    assert spec.annotations["readOnlyHint"] is False
    assert spec.schema["required"] == ["card", "decision", "said"]
    assert "the tap finishes it" in spec.description  # no second call, no "done"
    assert "call again right away" not in spec.description


def test_without_a_doorkeeper_there_is_no_decide_tool(door):
    tools = catalog.Tools(
        "grace",
        ledger_root=door["church"]["ledger_root"],
        viewer=viewer_for("grace", "carol-jennings", tenants_root=door["church"]["root"]),
    )
    assert "decide_card" not in [s.name for s in tools.specs()]


# ---- Carol approves Ruth's report ---------------------------------------------------


def test_the_first_call_hands_back_a_link_for_carols_phone(door):
    card = _ruths(door["church"])["id"]
    said = _decide(_carol(door), card)
    assert said["status"] == "tap_to_confirm"
    assert said["link"] == "https://tim.example.org/yes#link-1"
    (asked,) = door["desk"].asked
    assert asked["person"] == "carol-jennings"
    assert asked["card"] == str(card) and asked["verb"] == "approve"
    assert "Ruth Hollis" in asked["summary"] and "$10.00" in asked["summary"]
    assert _ruths(door["church"])["status"] == "pending"


@pytest.mark.parametrize(("decision", "label"), [("approve", "approve"), ("reject", "decline")])
def test_the_link_comes_ready_as_one_short_tap(door, decision, label):
    # Real-phone check 2026-10-10: the bare one-time address filled the chat.
    # The tool hands back the link already labeled, and asks for it as is.
    card = _ruths(door["church"])["id"]
    said = _decide(_carol(door), card, decision)
    tap = f"[Tap to {label} with Face ID](https://tim.example.org/yes#link-1)"
    assert said["show"] == tap
    assert "as it is" in said["say"]
    again = _decide(_carol(door), card, decision)
    assert again["status"] == "waiting" and again["show"] == tap


def test_the_reply_says_the_address_stays_inside_the_link(door):
    # Variance 2026-10-12, GPT-5.4-mini, 5 of 20: it said "Tap to approve
    # with Face ID." with the address dropped, so the phone had nothing to
    # tap. "Never the bare address" read as "no address"; say what to keep.
    said = _decide(_carol(door), _ruths(door["church"])["id"], "approve")
    assert "Markdown link" in said["say"]
    assert "address inside the parentheses" in said["say"]
    assert "link and all" in catalog.DECIDE.description


def test_the_tap_finishes_it_and_carol_never_goes_back_to_the_ai(door):
    # Design day 2026-10-11 (engine #480): Face ID already proved it was
    # Carol; the engine writes the moment the doorkeeper holds her yes.
    card = _ruths(door["church"])["id"]
    first = _decide(_carol(door), card)
    assert "the page finishes it" in first["say"]
    assert "call decide_card again" not in first["say"]
    yes = door["desk"].tap()
    assert _finish(door["church"], door["desk"]) == set()
    assert _ruths(door["church"])["status"] == "approved"
    assert door["desk"].settled == [
        (yes, "approved", "Approved: an expense report for Ruth Hollis, $10.00.")
    ]


def test_asking_after_the_tap_hears_it_is_already_done(door):
    # "Hey, did this happen?" Yes.
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    door["desk"].tap()
    _finish(door["church"], door["desk"])
    said = _decide(_carol(door), card)
    assert said["status"] == "approved"
    assert said["say"] == (
        "It's already done. Approved: an expense report for Ruth Hollis, $10.00."
    )  # plain words, no card number
    assert len(door["desk"].asked) == 1


def test_asking_between_the_tap_and_the_write_hears_it_is_finishing(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    door["desk"].tap()
    said = _decide(_carol(door), card)
    assert said["status"] == "finishing" and "tapped" in said["say"]
    assert len(door["desk"].asked) == 1
    assert _ruths(door["church"])["status"] == "pending"


def test_asking_again_before_the_tap_is_still_waiting_on_the_same_link(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    said = _decide(_carol(door), card)
    assert said["status"] == "waiting" and said["link"].endswith("#link-1")
    assert "no rush" in said["say"] and "30 minutes" in said["say"]
    assert len(door["desk"].asked) == 1
    assert _ruths(door["church"])["status"] == "pending"


def test_a_timed_out_link_is_replaced_with_a_kind_word(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    door["desk"].lapse()
    said = _decide(_carol(door), card)
    assert said["status"] == "tap_to_confirm" and said["link"].endswith("#link-2")
    assert "timed out" in said["say"] and "keeps the yes safe" in said["say"]


def test_after_the_tap_the_card_is_approved_by_the_door(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    door["desk"].tap()
    _finish(door["church"], door["desk"])
    row = _ruths(door["church"])
    assert row["status"] == "approved"
    assert row["params"]["decided_by"] == "carol-jennings"
    assert row["params"]["decided_via"] == "door"
    assert json.loads(row["params"]["witness"])["signature"] == "s"


def test_a_no_by_face_id_rejects(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card, "reject")
    door["desk"].tap()
    _finish(door["church"], door["desk"])
    assert _ruths(door["church"])["status"] == "rejected"
    assert door["desk"].settled[-1][1:] == (
        "rejected",
        "Declined: an expense report for Ruth Hollis, $10.00.",
    )


def test_a_yes_from_someone_elses_phone_decides_nothing(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    door["desk"].tap(person="ruth-hollis")
    _finish(door["church"], door["desk"])
    ((_yes, status, message),) = door["desk"].settled
    assert status == "refused" and "phone that isn't yours" in message
    assert _ruths(door["church"])["status"] == "pending"


def test_a_yes_written_before_a_restart_but_not_settled_is_settled_once(door):
    # The engine wrote the card, then stopped before telling the doorkeeper.
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    yes = door["desk"].tap()
    tapped = door["desk"].tapped[yes][1]
    assert _finisher(door["church"], door["desk"]).finish(tapped)[0] == "approved"
    _finish(door["church"], door["desk"])  # after the restart
    assert door["desk"].settled == [
        (yes, "approved", "Approved: an expense report for Ruth Hollis, $10.00.")
    ]
    # her own decision, already in the books, reads as done, not as someone else's


def test_someone_else_deciding_first_is_said_plainly_on_the_page(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    door["desk"].tap()
    with Ledger.open(door["church"]["ledger_root"]) as ledger:
        ledger.conn.execute("UPDATE approval_queue SET status = 'approved' WHERE id = ?", (card,))
        ledger.conn.commit()
    _finish(door["church"], door["desk"])
    ((_yes, status, message),) = door["desk"].settled
    assert status == "refused" and message == "Someone already decided this expense report."


def test_the_finisher_is_started_only_for_a_tenant_that_opened_the_door(church):  # noqa: F811
    args = {"tenants_root": church["root"], "ledger_root": church["ledger_root"]}
    assert finisher_for("grace", desk=FakeDesk(), **args) is None
    _open_door(church, "expense.report")
    assert isinstance(finisher_for("grace", desk=FakeDesk(), **args), Finisher)
    assert finisher_for("grace", desk=None, **args) is None


def test_the_finisher_keeps_going_when_the_doorkeeper_is_away(door):
    class Away(FakeDesk):
        def next(self, skip, wait):
            raise DeskRefused("unreachable")

    paused: list[float] = []
    finisher = Finisher(
        "grace",
        tenants_root=door["church"]["root"],
        ledger_root=door["church"]["ledger_root"],
        desk=Away(),
        sleep=paused.append,
    )
    finisher.step(set())
    assert paused == [finisher.pause]


# ---- refused before anyone's phone is asked ---------------------------------------


def test_ruth_cannot_approve_her_own_and_her_phone_is_never_asked(door):
    tools = _tools(door["church"], "ruth-hollis", door["desk"])
    with pytest.raises(ValueError):
        _decide(tools, _ruths(door["church"])["id"])
    assert door["desk"].asked == []


def test_a_card_the_person_cannot_see_is_not_waiting_for_them(door):
    bakers = _cards(door["church"])["Tom and Jen Baker"]["id"]
    tools = _tools(door["church"], "ruth-hollis", door["desk"])
    with pytest.raises(ValueError, match="no card"):
        _decide(tools, bakers)
    assert door["desk"].asked == []


def test_a_resource_the_door_does_not_open_is_decided_at_a_terminal(church):  # noqa: F811
    _open_door(church, "vendor")
    desk = FakeDesk()
    with pytest.raises(ValueError, match="ministry's own computer"):
        _decide(_tools(church, "carol-jennings", desk), _ruths(church)["id"])
    assert desk.asked == []


@pytest.mark.parametrize(
    ("reason", "said"), [("no_passkey", "welcome link"), ("unreachable", "try again")]
)
def test_when_no_link_can_be_made_the_person_hears_what_to_do(door, reason, said):
    door["desk"].refuse = reason
    with pytest.raises(ValueError, match=said):
        _decide(_carol(door), _ruths(door["church"])["id"])


@pytest.mark.parametrize(
    "args", [{"card": "x", "decision": "approve"}, {"card": 1, "decision": "pay"}]
)
def test_a_garbled_request_is_refused(door, args):
    with pytest.raises(ValueError):
        _carol(door).call("decide_card", args)


def test_while_a_run_holds_the_books_the_yes_waits_and_is_written_after(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    yes = door["desk"].tap()
    held: set[int] = set()
    with ledger_write_lock(door["church"]["ledger_root"]):
        t = threading.Thread(target=lambda: held.update(_finish(door["church"], door["desk"])))
        t.start()
        t.join(10)
    assert held == {yes} and door["desk"].settled == []  # kept, never lost
    assert _ruths(door["church"])["status"] == "pending"
    assert _finish(door["church"], door["desk"], frozenset(held)) == set()
    assert _ruths(door["church"])["status"] == "approved"  # no second tap


# ---- a human-only card -------------------------------------------------------------


def _new_vendor_card(church) -> int:  # noqa: F811
    with Ledger.open(church["ledger_root"]) as ledger:
        run_id = ledger.conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        ledger.enqueue_approval(
            idempotency_key="new-vendor",
            run_id=run_id,
            tenant="grace",
            agent="ap",
            action_type=NEW_VENDOR_CARD,
            params={"vendor": "Marion Roofing", "human_only": "true"},
        )
    return _cards(church)[NEW_VENDOR_CARD]["id"]


def test_a_human_only_card_on_an_opened_resource_is_decided_by_the_door(church):  # noqa: F811
    _open_door(church, "vendor")
    card, desk = _new_vendor_card(church), FakeDesk()
    asks = AskBook()
    _decide(_tools(church, "carol-jennings", desk, asks), card)
    desk.tap()
    _finish(church, desk)
    params = _cards(church)[NEW_VENDOR_CARD]["params"]
    assert params["decided_via"] == "door" and params["decided_by"] == "carol-jennings"


def test_a_human_only_card_the_door_does_not_open_stays_at_the_terminal(door):
    card = _new_vendor_card(door["church"])
    with pytest.raises(ValueError, match="ministry's own computer"):
        _decide(_carol(door), card)
    assert door["desk"].asked == []


# ---- over the HTTP door -------------------------------------------------------------


def test_over_the_http_door_carol_is_offered_decide_card_and_told_how_it_works(door, tmp_path):
    from core.tools.mcp_http import TokenFile, make_server, new_token
    from tests.unit.test_mcp_over_http import RESOURCE, _rpc

    tokens = tmp_path / "door-tokens.json"
    carol, ruth = new_token(tokens, "carol-jennings"), new_token(tokens, "ruth-hollis")
    server = make_server(
        "grace",
        tenants_root=door["church"]["root"],
        ledger_root=door["church"]["ledger_root"],
        obligations_file=door["church"]["obligations"],
        verifier=TokenFile(tokens),
        resource=RESOURCE,
        authorization_servers=["https://signin.example.org"],
        port=0,
        desk=door["desk"],
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    box = {"base": f"http://127.0.0.1:{server.server_address[1]}"}
    try:
        hello = _rpc(box, carol, "initialize", {"protocolVersion": "2025-06-18"})
        assert "decide_card" in hello["result"]["instructions"]
        assert "the tap finishes it" in hello["result"]["instructions"]
        listed = _rpc(box, carol, "tools/list")["result"]["tools"]
        (tool,) = [t for t in listed if t["name"] == "decide_card"]
        assert tool["annotations"]["readOnlyHint"] is False
        card = _ruths(door["church"])["id"]
        args = {"name": "decide_card", "arguments": {"card": card, "decision": "approve"}}
        first = json.loads(_rpc(box, carol, "tools/call", args)["result"]["content"][0]["text"])
        assert first["status"] == "tap_to_confirm"
        door["desk"].tap()
        _finish(door["church"], door["desk"])
        done = json.loads(_rpc(box, carol, "tools/call", args)["result"]["content"][0]["text"])
        assert done["status"] == "approved" and "already" in done["say"]
        refused = _rpc(box, ruth, "tools/call", args)["result"]
        assert refused["isError"] is True
    finally:
        server.shutdown()
        server.server_close()


# ---- a vouched yes ------------------------------------------------------------------


def _vouch(church, line: str) -> None:  # noqa: F811
    path = church["root"] / "grace" / "authority.toml"
    path.write_text(path.read_text() + f"\n[vouch]\n{line}\n")


def test_with_no_passkey_carols_yes_goes_to_the_person_named_to_vouch(door):
    _vouch(door["church"], 'carol-jennings = "don-pruitt"')
    door["desk"].no_passkey = {"carol-jennings"}
    card = _ruths(door["church"])["id"]
    said = _decide(_carol(door), card)
    assert said["status"] == "tap_to_confirm" and said["vouched_by"] == "Don Pruitt"
    assert "finishes it" in said["say"] and "tell me" not in said["say"]
    (asked,) = door["desk"].asked
    assert asked["person"] == "don-pruitt" and asked["for"] == "carol-jennings"
    assert asked["summary"].startswith("Carol Jennings asked you to confirm")
    assert "Ruth Hollis" in asked["summary"]
    door["desk"].tap()
    _finish(door["church"], door["desk"])
    assert door["desk"].settled[-1][1] == "approved"
    params = _ruths(door["church"])["params"]
    assert params["decided_by"] == "carol-jennings"
    assert params["decided_via"] == "door-vouched"
    assert params["vouched_by"] == "don-pruitt"


def test_with_no_passkey_and_no_one_named_the_person_hears_what_to_do(door):
    door["desk"].no_passkey = {"carol-jennings"}
    with pytest.raises(ValueError, match="welcome link"):
        _decide(_carol(door), _ruths(door["church"])["id"])


def test_a_vouched_yes_decides_a_human_only_card_the_same_as_face_id(church):  # noqa: F811
    _open_door(church, "vendor")
    _vouch(church, 'carol-jennings = "don-pruitt"')
    card, desk, asks = _new_vendor_card(church), FakeDesk(), AskBook()
    desk.no_passkey = {"carol-jennings"}
    said = _decide(_tools(church, "carol-jennings", desk, asks), card)
    assert said["vouched_by"] == "Don Pruitt"
    desk.tap()
    _finish(church, desk)
    assert desk.settled[-1][1] == "approved"
    params = _cards(church)[NEW_VENDOR_CARD]["params"]
    assert params["decided_via"] == "door-vouched" and params["vouched_by"] == "don-pruitt"
    assert params["witness_resource"] == "vendor"


def test_what_the_engine_records_is_what_the_auditor_stands_behind(church):  # noqa: F811
    """A door decision on a human-only card, own Face ID and vouched, read
    back by the auditor's own check (#465): the engine's stamps and the
    auditor's reading cannot drift apart."""
    import base64
    from datetime import UTC, datetime

    from auditor.lenses import AuditContext
    from auditor.lenses.approvals import decision_challenge, door_refusal

    def b64u(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    _open_door(church, "vendor")
    _vouch(church, 'carol-jennings = "don-pruitt"')
    for tapper, key in (("carol-jennings", "own"), ("don-pruitt", "vouched")):
        with Ledger.open(church["ledger_root"]) as ledger:
            run_id = ledger.conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            ledger.enqueue_approval(
                idempotency_key=f"new-vendor-{key}",
                run_id=run_id,
                tenant="grace",
                agent="ap",
                action_type=NEW_VENDOR_CARD,
                params={"vendor": f"Roofer {key}", "human_only": "true"},
            )
        card = next(r for r in _all(church) if r["params"].get("vendor") == f"Roofer {key}")["id"]
        desk, asks = FakeDesk(), AskBook()
        if tapper != "carol-jennings":
            desk.no_passkey = {"carol-jennings"}
        _decide(_tools(church, "carol-jennings", desk, asks), card)
        asked = desk.asked[-1]
        nonce = b"n" * 32
        signed = decision_challenge(nonce, tapper, str(card), "approve", asked["summary"])
        client = {"type": "webauthn.get", "challenge": b64u(signed), "origin": "https://x"}
        desk.tap(
            evidence={
                "nonce": b64u(nonce),
                "credential_id": "c",
                "public_key": "k",
                "client_data": b64u(json.dumps(client).encode()),
                "authenticator_data": "a",
                "signature": "s",
            },
        )
        _finish(church, desk)
        assert desk.settled[-1][1] == "approved"
        row = next(r for r in _all(church) if r["id"] == card)
        ctx = AuditContext(
            tenant=SimpleNamespace(slug="grace"),
            ledger=None,
            now=datetime.now(UTC),
            store_root=church["root"],
            tenants_dir=church["root"],
        )
        assert door_refusal(ctx, {"id": card, "status": "approved"}, row["params"]) == ""


def _all(church) -> list[dict]:  # noqa: F811
    with Ledger.open(church["ledger_root"]) as ledger:
        return ledger.list_approvals("grace")


def test_the_door_asks_for_the_persons_words_not_the_machinery():
    # Real-phone check 2026-10-11: Carol read "Card 5 is..." and "the expense
    # reports list came back empty". The person's AI speaks in her terms;
    # card numbers, sources, and what a lookup returned stay in its calls.
    from core.tools.mcp_http import DOOR_INSTRUCTIONS

    assert "never in your words" in DOOR_INSTRUCTIONS
    said = DOOR_INSTRUCTIONS.lower()
    assert "card numbers" in said and "what a lookup returned" in said
    assert "cite its source" not in DOOR_INSTRUCTIONS
    assert ":w" not in DOOR_INSTRUCTIONS and "  " not in DOOR_INSTRUCTIONS  # joined as prose
    assert "dollar line" in DOOR_INSTRUCTIONS


# ---- the dollar line: a yes by word (the owner's decision of 2026-10-11) -----------
# "$500 for Grace": at or under the ministry's line, the person's word to
# their own AI decides the card, with no link and no tap. Above it, a card
# with no amount, a card that commits no money, or a human-only card still
# takes their own Face ID.


def test_onboarding_gives_each_shape_its_default_line(church):  # noqa: F811
    from core.authority import parse_policy
    from core.onboarding import _question

    q = _question("yes_by_word_up_to")
    defaults = {
        s: q.default_for(s) for s in ("nonprofit-solo", "nonprofit-small", "nonprofit-organization")
    }
    assert defaults == {
        "nonprofit-solo": "250",
        "nonprofit-small": "500",
        "nonprofit-organization": "1000",
    }
    import tomllib

    data = tomllib.loads((church["root"] / "grace" / "authority.toml").read_text())
    assert str(parse_policy(data).safeguards.yes_by_word_up_to) == "500"


def test_under_the_line_carols_word_decides_it_with_no_link(door):
    _word_line(door["church"], "500")
    card = _ruths(door["church"])["id"]
    said = _decide(_carol(door), card, said="Approve Ruth's report")
    assert door["desk"].asked == []  # no link, no phone
    assert said["status"] == "approved" and "link" not in said
    assert said["say"].startswith("Done. Approved: an expense report for Ruth Hollis, $10.00.")
    row = _ruths(door["church"])
    assert row["status"] == "approved"
    assert row["params"]["decided_by"] == "carol-jennings"
    assert row["params"]["decided_via"] == "door-word"
    assert row["params"]["yes_by_word_up_to"] == "500"
    assert "witness" not in row["params"]


def test_under_the_line_a_no_is_by_word_too(door):
    _word_line(door["church"], "500")
    said = _decide(_carol(door), _ruths(door["church"])["id"], "reject", "Decline Ruth's")
    assert said["status"] == "rejected" and door["desk"].asked == []
    assert _ruths(door["church"])["status"] == "rejected"


def test_over_the_line_it_takes_her_face_id(door):
    _word_line(door["church"], "5")  # Ruth's card is $10.00
    said = _decide(_carol(door), _ruths(door["church"])["id"], said="Approve Ruth's report")
    assert said["status"] == "tap_to_confirm" and len(door["desk"].asked) == 1
    assert _ruths(door["church"])["status"] == "pending"


def test_at_the_line_exactly_her_word_decides_it(door):
    _word_line(door["church"], "10")
    said = _decide(_carol(door), _ruths(door["church"])["id"], said="Approve Ruth's report")
    assert said["status"] == "approved"


def test_a_human_only_card_under_the_line_still_takes_her_face_id(church):  # noqa: F811
    _open_door(church, "vendor")
    _word_line(church, "500")
    card, desk = _new_vendor_card(church), FakeDesk()
    said = _decide(_tools(church, "carol-jennings", desk), card, said="Approve the new vendor")
    assert said["status"] == "tap_to_confirm" and len(desk.asked) == 1


def test_ruth_still_cannot_approve_her_own_by_word(door):
    _word_line(door["church"], "500")
    tools = _tools(door["church"], "ruth-hollis", door["desk"])
    with pytest.raises(ValueError):
        _decide(tools, _ruths(door["church"])["id"])
    assert _ruths(door["church"])["status"] == "pending"


def test_while_a_run_holds_the_books_a_yes_by_word_falls_back_to_the_tap(door):
    _word_line(door["church"], "500")
    with ledger_write_lock(door["church"]["ledger_root"]):
        said = _decide(_carol(door), _ruths(door["church"])["id"], said="Approve Ruth's")
    assert said["status"] == "tap_to_confirm"  # her tap waits safely with the doorkeeper


def test_the_gate_refuses_a_word_on_a_resource_the_door_does_not_open(church):  # noqa: F811
    from datetime import UTC, datetime

    from core.engine.authority_gate import decide_card, load_policy

    _word_line(church, "500")  # the line alone opens nothing: [presence] door does
    policy = load_policy("grace", tenants_root=church["root"]).policy
    row = {**_ruths(church), "params": dict(_ruths(church)["params"])}
    now = datetime.now(UTC)
    ruling = decide_card(
        policy,
        "carol-jennings",
        row,
        decision="approved",
        at_terminal=False,
        delegations=[],
        today=now.date(),
        now=now.isoformat(),
        by_word=True,
    )
    assert "terminal" in ruling.refusal


def test_decide_card_tells_the_ai_to_speak_plainly():
    # Real-phone check 2026-10-11, card #6: the Claude app does not pass the
    # door's instructions to the model, so the plain-words rule rides in the
    # tool's own description, which every client shows it.
    text = catalog.DECIDE.description.lower()
    assert "never the card number" in text


# ---- her words name the card (the owner's decision of 2026-10-12) ------------------
# Proving-ground's agency run: told "approve everything waiting for Africa"
# (none of it hers), a small model approved an under-the-line report by word.
# The word decides a card only when the person's own words name whose it is
# (a first name, unless two people share it; then first and last) or how
# much. Anything else takes their Face ID, so a yes aimed wrong costs a tap
# she can see instead of going through.


def _word(door, words: str, decision: str = "approve") -> dict:
    _word_line(door["church"], "500")
    return _decide(_carol(door), _ruths(door["church"])["id"], decision, words)


@pytest.mark.parametrize(
    "words",
    [
        "Approve Ruth's report",
        "yes go ahead and approve ruths",
        "aprove ruth hollis report",
        "Please approve the expense report for Ruth Hollis.",
        "ok the $10 one, approve it",
        "the 10.00 one is fine",
    ],
)
def test_words_that_name_her_or_the_amount_decide_it(door, words):
    said = _word(door, words)
    assert said["status"] == "approved" and door["desk"].asked == []
    assert _ruths(door["church"])["params"]["said"] == words  # kept for the auditor


@pytest.mark.parametrize(
    "words",
    [
        "Approve everything waiting for Africa",
        "approve them all",
        "yes",
        "the $686.40 one",  # an amount that is not this card's
        "approve my report",  # Carol's "my" is not Ruth's card
        "",
    ],
)
def test_words_that_name_neither_take_her_face_id(door, words):
    said = _word(door, words)
    assert said["status"] == "tap_to_confirm" and len(door["desk"].asked) == 1
    assert _ruths(door["church"])["status"] == "pending"


def test_a_no_by_word_has_to_name_the_card_too(door):
    said = _word(door, "decline all of them", "reject")
    assert said["status"] == "tap_to_confirm"
    assert _ruths(door["church"])["status"] == "pending"


def test_a_first_name_two_people_share_needs_the_last_name(church):  # noqa: F811
    from dataclasses import replace
    from datetime import UTC, datetime

    from core.authority import Principal
    from core.engine.authority_gate import decide_card, load_policy

    _open_door(church, "expense.report")
    _word_line(church, "500")
    policy = load_policy("grace", tenants_root=church["root"]).policy
    other = Principal("ruth-ames", "person", ("missionary",))
    policy = replace(policy, people={**policy.people, "ruth-ames": other})
    row = {**_ruths(church), "params": dict(_ruths(church)["params"])}
    now = datetime.now(UTC)

    def ruling(words: str) -> str:
        return decide_card(
            policy,
            "carol-jennings",
            row,
            decision="approved",
            at_terminal=False,
            delegations=[],
            today=now.date(),
            now=now.isoformat(),
            by_word=True,
            said=words,
        ).refusal

    assert "Face ID" in ruling("Approve Ruth's report")
    assert ruling("Approve Ruth Hollis's report") == ""
    assert ruling("approve the $10.00 one") == ""


def test_decide_card_asks_for_the_persons_own_words():
    schema = catalog.DECIDE.schema
    assert "said" in schema["required"]
    assert "own words" in schema["properties"]["said"]["description"]


def test_after_a_link_for_vague_words_naming_the_card_decides_it_by_word(door):
    # Proving-ground's agency run: "approve everything waiting for Africa"
    # handed Ann a link for Mei's; her next words named Mei, and the waiting
    # link must not stand in for the word that now decides it.
    assert _word(door, "approve them all")["status"] == "tap_to_confirm"
    said = _decide(_carol(door), _ruths(door["church"])["id"], said="Approve Ruth's report")
    assert said["status"] == "approved" and len(door["desk"].asked) == 1
    assert _ruths(door["church"])["params"]["decided_via"] == "door-word"
    door["desk"].tap()  # she taps the old link anyway: her page hears it's done
    _finish(door["church"], door["desk"])
    assert door["desk"].settled[-1][1] == "approved"
