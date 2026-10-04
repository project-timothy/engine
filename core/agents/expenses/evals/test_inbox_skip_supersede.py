"""Nested inbox skip cards supersede older ones (built 2026-10-02).

Proposal: ``docs/proposals/2026-09-30-nested-inbox-skip-cards-supersede.md``
Candidate: 99e8ae57 (lens 19, ``approvals``/``stale-pending``: 4 subjects in 60
days, three of them nested receipt-inbox skip cards covering 2, 3 and 5 files,
every set contained in the next, all three answered by hand after the superset
card had already answered them).

The nesting is structural, not bad luck: the files only leave the inbox root
when the owner APPROVES the card, so tonight's unanswered set is still there
tomorrow and tomorrow's card covers it plus whatever arrived since. Different
set, different key, so the queue's dedup correctly declines to collapse the two
and both stay pending. The contract this row completes is already written down —
a re-ask rides a fresh key NAMING the card it supersedes — and the lane
implements the first half of that sentence and not the second.

Two contracts, written before the code exists:

1. a new skip card retires every pending skip card whose file set it already
   covers, as ``superseded`` and never as ``approved``, so one answer answers
   them all and nothing executes on a closed card's behalf;
2. containment decides it, never recency. A pending card holding one file the
   new set does not cover keeps asking about that file.

Fixtures are neutral placeholders on purpose: this tree is hermetic and names no
tenant, person, vendor, or image. Digests are short stand-ins for the sha256
list the card already carries in its ``sha256s`` param.
"""

from __future__ import annotations

# Placeholder digests standing in for the sha256 list the card already carries.
A, B, C, D, E = "aa", "bb", "cc", "dd", "ee"
F, G, H, J = "ff", "a1", "b2", "c3"


def _lane():
    """The containment test this row adds. Absent today: ``_inbox_run`` parks
    its fresh card and leaves every older pending card exactly where it was."""
    from core.agents.expenses import jobs

    return jobs


def _card(card_id: int, *, shas: tuple[str, ...], action: str = "", status: str = "pending"):
    """An ``approval_queue`` row as the lane reads its own pending cards: the
    digest list is already in the params, and is already what the key is built
    from."""
    lane = _lane()
    return {
        "id": card_id,
        "action_type": action or lane.INBOX_SKIP_CARD,
        "status": status,
        "key": f"inboxskip:card{card_id}",
        "params": {"count": str(len(shas)), "sha256s": ",".join(sorted(shas))},
    }


def test_nested_skip_cards_are_superseded_not_answered_one_by_one():
    lane = _lane()

    # The live September shape: each night's set contains the night before's.
    night1 = _card(1, shas=(A, B))
    night2 = _card(2, shas=(A, B, C))
    night3 = _card(3, shas=(A, B, C, D, E))
    tonight = (A, B, C, D, E, F, G, H, J)

    plan = lane.skip_card_supersessions([night1, night2, night3], tonight)

    assert [entry["card_id"] for entry in plan] == [1, 2, 3], (
        "every pending skip card whose files tonight's card already covers is "
        "retired by it, oldest first"
    )
    assert [entry["key"] for entry in plan] == [
        "inboxskip:card1",
        "inboxskip:card2",
        "inboxskip:card3",
    ], "the plan names the card it closes, so the declaration can travel on the contract"

    # The lane DECLARES; only the runner may close a queue row (the runner owns
    # all persistence, so an agent never touches the queue's write internals).
    from core.engine.contracts import ApprovalSpec

    spec = ApprovalSpec(
        key="inboxskip:tonight",
        action_type=lane.INBOX_SKIP_CARD,
        params={"count": str(len(tonight)), "sha256s": ",".join(sorted(tonight))},
        supersedes_keys=[entry["key"] for entry in plan],
    )
    assert spec.supersedes_keys == ["inboxskip:card1", "inboxskip:card2", "inboxskip:card3"]

    from core.engine import runner

    assert runner.SUPERSEDED_STATUS == "superseded"
    assert runner.SUPERSEDED_STATUS != "approved", (
        "a superseded card is queue hygiene, never a decision: the lane's "
        "execute path selects approved cards, so a closed card moves no file"
    )


def test_a_card_holding_a_file_the_new_set_lacks_is_never_superseded():
    lane = _lane()

    # A file the owner moved out of the inbox by hand leaves tonight's set
    # SMALLER, and the card that still asks about it must keep asking.
    held = _card(10, shas=(A, B, F))
    # An equal set is the queue's dedup problem, already solved: equal sets mean
    # equal keys, and closing that card would undo the memory dedup just kept.
    equal = _card(11, shas=(A, B, C))
    # A different question about a different file is never a subset of this one.
    other = _card(12, shas=(A,), action=lane.INBOX_FILE_CARD)
    # An answered card is a fact, not a candidate.
    answered = _card(13, shas=(A, B), status="approved")
    tonight = (A, B, C)

    plan = lane.skip_card_supersessions([held, equal, other, answered], tonight)

    assert plan == [], (
        "containment decides supersession, never recency: a held file, an equal "
        "set, another lane's question, and an answered card are all left alone"
    )


# ---- end to end through the inbox lane --------------------------------------


def _harness():
    from . import test_inbox as h

    return h


def _not_receipt(tmp_path, name, body):
    return _harness()._seed(tmp_path, name, body, {"receipt": False, "confidence": 0.1})


def _cards(tmp_path):
    h = _harness()
    with h._ledger(tmp_path) as ledger:
        return [
            (c["id"], c["status"], c["params"]["count"])
            for c in ledger.list_approvals("demo")
            if c["action_type"] == "expenses.inbox_skip"
        ]


def test_unanswered_nights_leave_exactly_one_pending_skip_card(tmp_path):
    h = _harness()
    _not_receipt(tmp_path, "IMG_0001.jpg", b"one")
    _not_receipt(tmp_path, "IMG_0002.jpg", b"two")
    h._run_inbox(tmp_path)
    _not_receipt(tmp_path, "IMG_0003.jpg", b"three")
    second = h._run_inbox(tmp_path)

    cards = _cards(tmp_path)
    assert [(s, n) for _, s, n in cards] == [("superseded", "2"), ("pending", "3")], (
        "the second night's card covers the first night's files, so the first "
        "card retires as superseded and one question stays on the table"
    )
    assert "covers 1 older pending" in second.summary, (
        "the lane says what it computed; the runner's event records what it closed"
    )

    h._approve(tmp_path, cards[-1][0])
    h._run_inbox(tmp_path)
    kept = tmp_path / "inbox" / "_not-receipts"
    assert sorted(p.name for p in kept.iterdir() if p.suffix == ".jpg") == [
        "IMG_0001.jpg",
        "IMG_0002.jpg",
        "IMG_0003.jpg",
    ], "one approval answers every file the closed card asked about"

    h2 = h._ledger(tmp_path)
    with h2 as ledger:
        rows = ledger.conn.execute(
            "SELECT payload_json FROM events WHERE event_type = 'engine.approval_superseded'"
        ).fetchall()
    assert len(rows) == 1, "the supersession is an event naming both cards"


def test_a_hand_moved_file_keeps_its_card_asking(tmp_path):
    h = _harness()
    _not_receipt(tmp_path, "IMG_0001.jpg", b"one")
    _not_receipt(tmp_path, "IMG_0002.jpg", b"two")
    h._run_inbox(tmp_path)
    (tmp_path / "inbox" / "IMG_0002.jpg").unlink()  # the owner moved it by hand
    _not_receipt(tmp_path, "IMG_0003.jpg", b"three")
    h._run_inbox(tmp_path)

    assert [s for _, s, _ in _cards(tmp_path)] == ["pending", "pending"], (
        "tonight's set lacks a file the first card asks about, so containment "
        "fails and the first card keeps asking"
    )
