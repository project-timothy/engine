"""Deciding a card through the box's door, with the person's own Face ID
(engine #465, step 2b; the owner's decision of 2026-10-09: "approve it" in
the chat or an Approve button, and Face ID proves it was the person).

Carol, Grace Fellowship's treasurer, tells her AI to approve Ruth's report.
The AI calls ``decide_card``. The engine first checks Carol may decide it at
all, so her phone is never asked for something she cannot approve; then it
asks the doorkeeper for a one-time link and hands it back. Carol opens it,
reads the card, taps Approve, Face ID. The AI calls ``decide_card`` again;
the engine collects her yes and decides the card through the same path the
terminal uses, stamped as decided by the door with the signed evidence.

The tool is offered only on a tenant whose authority.toml opened the door
(``[presence] door``), and only on the HTTP door with a doorkeeper.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from core.agents.ap.jobs import NEW_VENDOR_CARD
from core.engine.authority_gate import Witnessed
from core.engine.runner import ledger_write_lock
from core.ledger import Ledger
from core.tools import catalog
from core.tools.decide import AskBook, Decider
from core.tools.viewer import viewer_for
from core.tools.witness_desk import Asked, DeskRefused
from tests.unit.test_tools_answer_as_a_person import church  # noqa: F401  (fixture)

EVIDENCE = {"nonce": "n", "credential_id": "c", "signature": "s"}


class FakeDesk:
    """The doorkeeper, standing in: it hands out links and, once the person
    taps (``tap``), answers with their witnessed yes."""

    def __init__(self) -> None:
        self.asked: list[dict] = []
        self.answer: Witnessed | str = "waiting"
        self.refuse = ""
        self.no_passkey: set[str] = set()

    def ask(self, person: str, card: str, verb: str, summary: str) -> Asked:
        if self.refuse:
            raise DeskRefused(self.refuse)
        if person in self.no_passkey:
            raise DeskRefused("no_passkey")
        self.asked.append({"person": person, "card": card, "verb": verb, "summary": summary})
        n = len(self.asked)
        return Asked(f"witness-{n}", f"https://tim.example.org/yes#link-{n}")

    def check(self, witness: str) -> Witnessed | str:
        answer = self.answer
        if isinstance(answer, Witnessed):
            self.answer = "unknown"  # the doorkeeper hands a yes over once
        return answer

    def tap(self, **over) -> None:
        last = {**self.asked[-1], **over}
        self.answer = Witnessed(
            last["person"], last["card"], last["verb"], last["summary"], 1_800_000_000.0, EVIDENCE
        )


def _open_door(church, *resources: str) -> None:  # noqa: F811
    path = church["root"] / "grace" / "authority.toml"
    listed = ", ".join(f'"{r}"' for r in resources)
    path.write_text(path.read_text() + f"\n[presence]\ndoor = [{listed}]\n")


def _tools(church, person: str, desk: FakeDesk, asks: AskBook | None = None) -> catalog.Tools:  # noqa: F811
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


def _decide(tools: catalog.Tools, card: int, decision: str = "approve") -> dict:
    return tools.call("decide_card", {"card": card, "decision": decision})


# ---- where the tool is offered ----------------------------------------------------


def test_a_tenant_that_did_not_open_the_door_is_not_offered_it(church):  # noqa: F811
    tools = _tools(church, "carol-jennings", FakeDesk())
    assert "decide_card" not in [s.name for s in tools.specs()]


def test_with_the_door_open_the_tool_is_offered_and_says_it_writes(door):
    (spec,) = [s for s in _carol(door).specs() if s.name == "decide_card"]
    assert spec.annotations["readOnlyHint"] is False
    assert spec.schema["required"] == ["card", "decision"]


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


def test_asking_again_before_the_tap_is_still_waiting_on_the_same_link(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    said = _decide(_carol(door), card)
    assert said["status"] == "waiting" and said["link"].endswith("#link-1")
    assert len(door["desk"].asked) == 1
    assert _ruths(door["church"])["status"] == "pending"


def test_after_the_tap_the_card_is_approved_by_the_door(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    door["desk"].tap()
    said = _decide(_carol(door), card)
    assert said["status"] == "approved", said
    row = _ruths(door["church"])
    assert row["status"] == "approved"
    assert row["params"]["decided_by"] == "carol-jennings"
    assert row["params"]["decided_via"] == "door"
    assert json.loads(row["params"]["witness"])["signature"] == "s"


def test_a_no_by_face_id_rejects(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card, "reject")
    door["desk"].tap()
    assert _decide(_carol(door), card, "reject")["status"] == "rejected"
    assert _ruths(door["church"])["status"] == "rejected"


def test_a_yes_from_someone_elses_phone_decides_nothing(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    door["desk"].tap(person="ruth-hollis")
    with pytest.raises(ValueError, match="phone that isn't yours"):
        _decide(_carol(door), card)
    assert _ruths(door["church"])["status"] == "pending"


def test_a_link_that_lapsed_is_replaced_with_a_fresh_one(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    door["desk"].answer = "unknown"
    said = _decide(_carol(door), card)
    assert said["status"] == "tap_to_confirm" and said["link"].endswith("#link-2")


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


def test_while_a_run_holds_the_books_the_person_is_told_to_try_again(door):
    card = _ruths(door["church"])["id"]
    _decide(_carol(door), card)
    door["desk"].tap()
    with ledger_write_lock(door["church"]["ledger_root"]):
        caught: list[Exception] = []

        def _try() -> None:
            try:
                _decide(_carol(door), card)
            except ValueError as exc:
                caught.append(exc)

        t = threading.Thread(target=_try)
        t.start()
        t.join(10)
    assert caught and "try again" in str(caught[0])
    assert _ruths(door["church"])["status"] == "pending"
    assert _decide(_carol(door), card)["status"] == "approved"  # the yes was kept


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
    assert _decide(_tools(church, "carol-jennings", desk, asks), card)["status"] == "approved"
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
        listed = _rpc(box, carol, "tools/list")["result"]["tools"]
        (tool,) = [t for t in listed if t["name"] == "decide_card"]
        assert tool["annotations"]["readOnlyHint"] is False
        card = _ruths(door["church"])["id"]
        args = {"name": "decide_card", "arguments": {"card": card, "decision": "approve"}}
        first = json.loads(_rpc(box, carol, "tools/call", args)["result"]["content"][0]["text"])
        assert first["status"] == "tap_to_confirm"
        door["desk"].tap()
        done = json.loads(_rpc(box, carol, "tools/call", args)["result"]["content"][0]["text"])
        assert done["status"] == "approved"
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
    (asked,) = door["desk"].asked
    assert asked["person"] == "don-pruitt"
    assert asked["summary"].startswith("Carol Jennings asked you to confirm")
    assert "Ruth Hollis" in asked["summary"]
    door["desk"].tap()
    assert _decide(_carol(door), card)["status"] == "approved"
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
    assert _decide(_tools(church, "carol-jennings", desk, asks), card)["status"] == "approved"
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
        desk.answer = Witnessed(
            tapper,
            str(card),
            "approve",
            asked["summary"],
            1_800_000_000.0,
            {
                "nonce": b64u(nonce),
                "credential_id": "c",
                "public_key": "k",
                "client_data": b64u(json.dumps(client).encode()),
                "authenticator_data": "a",
                "signature": "s",
            },
        )
        assert _decide(_tools(church, "carol-jennings", desk, asks), card)["status"] == "approved"
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
