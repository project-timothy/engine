"""start_here: who the person is, and Tim's answers for the tools they have.

The 10/14 phone run: asked who was signed in, Claude guessed the owner was
Ruth's reviewer, because no tool said who the person is. start_here says it
(their name, what they do, the ministry, whose spending they look over and
who looks over theirs) and hands over the FAQ entries for the tools this
person has, so people just ask (core/capabilities/faq.toml).
"""

from __future__ import annotations

from core.tools import catalog
from core.tools.receipts import ReceiptDesk, Receipts
from core.tools.viewer import viewer_for
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)

MINISTRY = "Living Water Mozambique, Inc."


def _tools(solo, person: str) -> catalog.Tools:  # noqa: F811
    return catalog.Tools(
        "living-water",
        ledger_root=solo["ledger_root"],
        viewer=viewer_for("living-water", person, tenants_root=solo["root"]),
        ministry=MINISTRY,
        receipts=Receipts(
            "living-water",
            tenants_root=solo["root"],
            ledger_root=solo["ledger_root"],
            person=person,
            desk=ReceiptDesk(),
            base_url="https://tim.example.org/receipt",
        ),
    )


def test_it_says_who_the_missionary_is_and_who_looks_over_his_spending(solo):  # noqa: F811
    you = _tools(solo, "daniel-reyes").call("start_here")["you"]
    assert you["name"] == "Daniel Reyes" and you["ministry"] == MINISTRY
    assert you["sends_receipts"] is True
    assert you["looked_over_by"] == ["Linda Park"] and you["looks_over"] == []


def test_it_says_whose_spending_the_reviewer_looks_over(solo):  # noqa: F811
    you = _tools(solo, "linda-park").call("start_here")["you"]
    assert you["name"] == "Linda Park" and you["sends_receipts"] is False
    assert you["looks_over"] == ["Daniel Reyes"]


def _ids(reply: dict) -> list[str]:
    return [a["id"] for a in reply["answers"] + reply.get("more", [])]


def test_each_person_hears_the_answers_for_their_own_tools(solo):  # noqa: F811
    daniel = _ids(_tools(solo, "daniel-reyes").call("start_here"))
    linda = _ids(_tools(solo, "linda-park").call("start_here"))
    assert daniel[0] == linda[0] == "always-allow"
    assert "send-a-receipt" in daniel and "send-a-receipt" not in linda
    assert "who-am-i" in daniel and "who-am-i" in linda


def test_every_signed_in_person_is_offered_it(solo):  # noqa: F811
    for person in ("daniel-reyes", "linda-park"):
        assert "start_here" in {s.name for s in _tools(solo, person).specs()}


def test_a_couple_reads_as_a_couple():
    from core.tools.viewer import display_name

    assert display_name("tom-and-jen-baker") == "Tom and Jen Baker"
    assert display_name("carol-jennings") == "Carol Jennings"


def test_it_fits_in_one_piece_and_hands_any_other_answer_on_asking(solo):  # noqa: F811
    """pass^5 on 2026-11-17: all 97 answers ran 65 KB, too big for the
    person's AI to take in one piece, so it read a cut-off preview."""
    import json

    tools = _tools(solo, "daniel-reyes")
    first = tools.call("start_here")
    assert len(json.dumps(first)) < 25_000
    assert first["answers"][0]["id"] == "always-allow" and "answer" in first["answers"][0]
    asked = next(m for m in first["more"] if m["id"] == "send-a-receipt")
    assert set(asked) == {"id", "ask"}
    got = tools.call("start_here", {"about": ["send-a-receipt"]})
    assert [a["id"] for a in got["answers"]] == ["send-a-receipt"] and got["answers"][0]["answer"]
    assert "more" not in got
