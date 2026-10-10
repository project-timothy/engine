"""A person decides when present: at a terminal, or witnessed by the box's
door (Face ID on their own phone, checked by the doorkeeper; engine #465,
step 2).

The promises under test:

1. ``[presence] door = [...]`` in authority.toml names the resources a
   person may decide through the door. Left out, it is empty: every tenant
   that does not opt in decides exactly as before.
2. A person's decision with no terminal counts only with a witness of that
   person, for that card, saying that answer, on a resource the tenant
   opened to the door. The decision records it was the door and carries the
   signed evidence.
3. ``WitnessDesk`` asks the doorkeeper for a yes and collects it, over
   localhost with the box's shared secret. Anything the engine cannot read
   as a yes is no yes.
"""

from __future__ import annotations

import json
import threading
import tomllib
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from core.agents.expenses.jobs import REVIEW_CARD
from core.authority import AuthorityError, parse_policy
from core.engine.authority_gate import Witnessed, decide_card
from core.tools.witness_desk import DeskRefused, WitnessDesk

POLICY = """
[money]
out = "human"

[roles.treasurer]
grants = ["view:*", "approve:*"]

[roles.missionary]
grants = ["view:*@own", "submit:expense.report@own"]

[people.carol-jennings]
roles = ["treasurer"]

[people.ruth-hollis]
roles = ["missionary"]
scopes = ["ruth-hollis"]
"""
DOOR = '\n[presence]\ndoor = ["expense.report"]\n'

CARD = {
    "id": 214,
    "agent": "expenses",
    "action_type": REVIEW_CARD,
    "params": {"person": "Ruth Hollis", "total_cents": "18640"},
    "created_at": "2026-10-09T12:00:00+00:00",
}
EVIDENCE = {"nonce": "n", "credential_id": "c", "signature": "s"}


def _witness(**over) -> Witnessed:
    fields = {
        "person": "carol-jennings",
        "card": "214",
        "verb": "approve",
        "summary": "Reimburse Ruth Hollis $186.40.",
        "at": 1_800_000_000.0,
        "evidence": EVIDENCE,
    }
    fields.update(over)
    return Witnessed(**fields)


def _decide(policy_text: str, principal: str = "carol-jennings", **over):
    args = {
        "decision": "approved",
        "at_terminal": False,
        "delegations": [],
        "today": date(2026, 10, 9),
        "now": "2026-10-09T13:00:00+00:00",
    }
    args.update(over)
    return decide_card(parse_policy(tomllib.loads(policy_text)), principal, CARD, **args)


# ---- 1. the switch -----------------------------------------------------------------


def test_left_out_the_door_opens_nothing():
    assert parse_policy(tomllib.loads(POLICY)).door == frozenset()


def test_the_door_names_resources():
    policy = parse_policy(tomllib.loads(POLICY + DOOR))
    assert policy.door == frozenset({"expense.report"})


@pytest.mark.parametrize(
    "table",
    ['door = ["expense.reports"]', 'door = "expense.report"', "door = [1]", 'elsewhere = ["x"]'],
)
def test_a_door_the_engine_cannot_read_is_refused(table):
    with pytest.raises(AuthorityError):
        parse_policy(tomllib.loads(POLICY + "\n[presence]\n" + table + "\n"))


# ---- 2. present = at a terminal, or witnessed by the door -------------------------


def test_a_person_with_no_terminal_and_no_witness_is_refused_as_before():
    out = _decide(POLICY + DOOR)
    assert out.refusal and "terminal" in out.refusal


def test_a_witnessed_yes_on_an_opened_resource_decides():
    out = _decide(POLICY + DOOR, witness=_witness())
    assert out.refusal == "" and out.final
    assert out.stamp["decided_by"] == "carol-jennings"
    assert out.stamp["decided_via"] == "door"
    assert out.stamp["witness_at"] == "2027-01-15T08:00:00+00:00"
    assert json.loads(out.stamp["witness"]) == {**EVIDENCE, "summary": _witness().summary}


def test_a_tenant_that_did_not_open_the_door_refuses_a_witness():
    out = _decide(POLICY, witness=_witness())
    assert "[presence] door" in out.refusal


@pytest.mark.parametrize(
    ("over", "said"),
    [
        ({"person": "ruth-hollis"}, "carol-jennings"),
        ({"card": "215"}, "#214"),
        ({"verb": "reject"}, "approve"),
    ],
)
def test_a_witness_of_anything_else_is_refused(over, said):
    out = _decide(POLICY + DOOR, witness=_witness(**over))
    assert out.refusal and said in out.refusal


def test_a_witnessed_no_rejects():
    out = _decide(POLICY + DOOR, decision="rejected", witness=_witness(verb="reject"))
    assert out.refusal == "" and out.final and out.stamp["decided_via"] == "door"


def test_at_a_terminal_nothing_about_the_door_is_stamped():
    out = _decide(POLICY + DOOR, at_terminal=True)
    assert out.final and "decided_via" not in out.stamp and "witness" not in out.stamp


# ---- 3. the engine's half: asking and collecting -----------------------------------

SECRET = "s" * 48


class _Doorkeeper(BaseHTTPRequestHandler):
    replies: dict[str, object] = {}
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802  the base class's name
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        form = {k: v[0] for k, v in parse_qs(body).items()}
        type(self).seen.append({"path": self.path, "auth": self.headers["Authorization"], **form})
        if self.headers.get("Authorization") != f"Bearer {SECRET}":
            self.send_response(401)
            self.end_headers()
            return
        status, reply = type(self).replies[self.path]
        raw = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def doorkeeper(tmp_path):
    _Doorkeeper.seen = []
    _Doorkeeper.replies = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Doorkeeper)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    secret = tmp_path / "introspect.secret"
    secret.write_text(SECRET + "\n")
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield {"base": base, "secret": secret}
    server.shutdown()
    server.server_close()


def _desk(d, **over) -> WitnessDesk:
    args = {"url": d["base"], "secret_file": d["secret"]}
    args.update(over)
    return WitnessDesk(**args)


def _witnessed_reply(**over) -> dict:
    reply = {
        "status": "witnessed",
        "person": "carol-jennings",
        "card": "214",
        "verb": "approve",
        "summary": "Reimburse Ruth Hollis $186.40.",
        "at": 1_800_000_000.0,
        "evidence": EVIDENCE,
    }
    reply.update(over)
    return reply


def test_asking_returns_the_link_for_the_person_and_keeps_the_witness_id(doorkeeper):
    _Doorkeeper.replies["/witness/ask"] = (
        200,
        {"witness": "w" * 43, "link": "https://tim.example/yes#t", "expires_in": 1800},
    )
    asked = _desk(doorkeeper).ask("carol-jennings", "214", "approve", "Reimburse Ruth.")
    assert asked.witness == "w" * 43 and asked.link == "https://tim.example/yes#t"
    (sent,) = _Doorkeeper.seen
    assert sent["auth"] == f"Bearer {SECRET}"
    assert {k: sent[k] for k in ("person", "card", "verb", "summary")} == {
        "person": "carol-jennings",
        "card": "214",
        "verb": "approve",
        "summary": "Reimburse Ruth.",
    }


@pytest.mark.parametrize("reason", ["no_passkey", "not_invited"])
def test_the_engine_hears_why_no_link_could_be_made(doorkeeper, reason):
    _Doorkeeper.replies["/witness/ask"] = (400, {"error": reason})
    with pytest.raises(DeskRefused) as refused:
        _desk(doorkeeper).ask("carol-jennings", "214", "approve", "x")
    assert refused.value.reason == reason


def test_a_doorkeeper_that_cannot_be_reached_is_a_refusal(tmp_path, doorkeeper):
    with pytest.raises(DeskRefused) as refused:
        _desk(doorkeeper, url="http://127.0.0.1:9").ask("carol-jennings", "214", "approve", "x")
    assert refused.value.reason == "unreachable"


def test_collecting_a_yes(doorkeeper):
    _Doorkeeper.replies["/witness/check"] = (200, _witnessed_reply())
    got = _desk(doorkeeper).check("w" * 43)
    assert got == _witness()
    assert _Doorkeeper.seen[0]["witness"] == "w" * 43


@pytest.mark.parametrize("status", ["waiting", "unknown"])
def test_no_yes_yet(doorkeeper, status):
    _Doorkeeper.replies["/witness/check"] = (200, {"status": status})
    assert _desk(doorkeeper).check("w" * 43) == status


@pytest.mark.parametrize(
    "reply",
    [
        b"not json",
        _witnessed_reply(status="Witnessed"),
        _witnessed_reply(person=""),
        _witnessed_reply(at="soon"),
        _witnessed_reply(evidence="signed"),
        _witnessed_reply(card=214),
    ],
)
def test_anything_the_engine_cannot_read_as_a_yes_is_no_yes(doorkeeper, reply):
    _Doorkeeper.replies["/witness/check"] = (200, reply)
    assert _desk(doorkeeper).check("w" * 43) == "unknown"


def test_a_wrong_secret_collects_nothing(doorkeeper, tmp_path):
    wrong = tmp_path / "wrong.secret"
    wrong.write_text("w" * 48)
    _Doorkeeper.replies["/witness/check"] = (200, _witnessed_reply())
    assert _desk(doorkeeper, secret_file=wrong).check("w" * 43) == "unknown"


@pytest.mark.parametrize(
    "url", ["http://tim.example.org", "ftp://127.0.0.1", "https://tim.example.org/?x=1"]
)
def test_the_doorkeeper_is_https_or_loopback(doorkeeper, url):
    with pytest.raises(ValueError):
        _desk(doorkeeper, url=url)


# ---- 4. a vouched yes ---------------------------------------------------------------
# Don's phone cannot do passkeys. He says "approve it" in the chat, and the
# person the ministry named for him (Carol) taps Approve with her own Face ID.

VOUCH = (
    POLICY.replace(
        "[people.ruth-hollis]", '[people.don-pruitt]\nroles = ["treasurer"]\n\n[people.ruth-hollis]'
    )
    + DOOR
    + '\n[vouch]\ndon-pruitt = "carol-jennings"\n'
)


def test_the_vouch_table_names_who_confirms_for_whom():
    assert parse_policy(tomllib.loads(VOUCH)).vouch == {"don-pruitt": "carol-jennings"}
    assert parse_policy(tomllib.loads(POLICY)).vouch == {}


@pytest.mark.parametrize(
    "line",
    [
        'don-pruitt = "nobody"',  # not a person
        'don-pruitt = "don-pruitt"',  # oneself
        'nobody = "carol-jennings"',
        "don-pruitt = 1",
    ],
)
def test_a_vouch_the_engine_cannot_hold_is_refused(line):
    text = VOUCH.replace('don-pruitt = "carol-jennings"', line)
    with pytest.raises(AuthorityError):
        parse_policy(tomllib.loads(text))


def test_carols_tap_decides_for_don_and_the_card_says_so():
    out = _decide(VOUCH, "don-pruitt", witness=_witness(person="carol-jennings"))
    assert out.refusal == "" and out.final
    assert out.stamp["decided_by"] == "don-pruitt"
    assert out.stamp["decided_via"] == "door-vouched"
    assert out.stamp["vouched_by"] == "carol-jennings"
    assert json.loads(out.stamp["witness"])["signature"] == "s"


def test_only_the_named_person_may_vouch():
    out = _decide(VOUCH, "don-pruitt", witness=_witness(person="ruth-hollis"))
    assert "ruth-hollis" in out.refusal


def test_someone_with_no_one_named_cannot_be_vouched_for():
    out = _decide(VOUCH, "carol-jennings", witness=_witness(person="don-pruitt"))
    assert out.refusal


def test_nobody_vouches_on_their_own_card():
    text = VOUCH.replace('don-pruitt = "carol-jennings"', 'don-pruitt = "ruth-hollis"')
    out = _decide(text, "don-pruitt", witness=_witness(person="ruth-hollis"))
    assert "own" in out.refusal


TWO = {"route": json.dumps([["treasurer", "two"]]), "route_step": "0", "route_votes": "[]"}


def _two_step(**params) -> dict:
    return {**CARD, "params": {**CARD["params"], **TWO, **params}}


def test_a_voucher_who_already_voted_cannot_also_vouch():
    voted = json.dumps([[0, "carol-jennings", "carol-jennings"]])
    out = decide_card(
        parse_policy(tomllib.loads(VOUCH)),
        "don-pruitt",
        _two_step(route_votes=voted),
        decision="approved",
        at_terminal=False,
        delegations=[],
        today=date(2026, 10, 9),
        now="2026-10-09T13:00:00+00:00",
        witness=_witness(person="carol-jennings"),
    )
    assert "already" in out.refusal


def test_a_voucher_cannot_then_vote_on_the_same_card_as_themselves():
    policy = parse_policy(tomllib.loads(VOUCH))
    args = {
        "decision": "approved",
        "delegations": [],
        "today": date(2026, 10, 9),
        "now": "2026-10-09T13:00:00+00:00",
    }
    vote = decide_card(
        policy,
        "don-pruitt",
        _two_step(),
        at_terminal=False,
        witness=_witness(person="carol-jennings"),
        **args,
    )
    assert vote.refusal == "" and not vote.final
    assert vote.stamp["vouchers"] == "carol-jennings"
    after = _two_step(**vote.stamp)
    out = decide_card(policy, "carol-jennings", after, at_terminal=True, **args)
    assert "vouched" in out.refusal
