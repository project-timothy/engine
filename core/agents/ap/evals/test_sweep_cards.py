"""ap: the bank-feed sweep's parked rows become approval cards.

Phase 7 row 7.4 (issue #213), the third card source. The weekly sweep of the
accounting system's bank feed clicks the matches it is allowed to click and
parks everything else in a note under a "Needs <owner>" heading, one bullet
per feed line with a proposal. Until now that note was the end of the road:
the owner read it, and whatever he did about it lived in his head until the
next sweep asked again.

This job reads the note back and turns each parked bullet into a
``qbo.sweep_parked`` card keyed by the feed line, so the answer lands in the
one place every other answer lands. The desk script then hands the next
sweep session the cards the owner approved, under "Post these".

Three properties are the contract, and each has cost something in the past:

* **The key is the feed line, not the note.** A row parked two weeks running
  is ONE question. The note's wording changes between sessions (a model
  writes it), so the key is built from the bank's own facts: the
  transaction date, the amount, the direction, and the bank text.
* **A decided card never re-asks.** Approved or rejected, the owner answered;
  re-parking it next Thursday is the nag this lane exists to remove.
* **The same physical check never cards twice across sources.** The
  hand-check lane (``ap.record_direct_payment``) sees the same check in the
  accounting feed and on the bank statement. A sweep bullet naming a check
  the owner has already been asked about is not a second question.

Fixtures here are synthetic by rule (invariant 5): no tenant, bank, vendor,
or path from a real book appears under ``core/``.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import unreadable
from core.agents.ap import sweep_note
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

SWEEP_CARD = "qbo.sweep_parked"
DP_CARD = "ap.record_direct_payment"

# A note in the shape the sweep writes: a tie-out, the clicks it made, and
# the rows it parked. Two parked rows, one of them a check.
TWO_PARKED = """# Sweep 2026-04-16

Run started 07:00 by the headless wrapper.

## Tie-out

- Operating Checking (1234): Bank $10,000.00 | Posted $9,000.00 | Pending 2

## Matched (unattended)

- 04/14/2026 | Operating Checking | ELECTRONIC IMAGE | $351.00 | Match -> Bill 20140

## Needs owner

- 04/15/2026 | Operating Checking | ELECTRONIC IMAGE | $1,371.44 spent | check 3027, no
  ledger row; hand-paid? | propose Project Expense | click: Find match, then Add
- 04/15/2026 | Operating Checking | CARD SERVICES AUTOPAY | $2,954.43 spent | the card's
  monthly autopay | propose a Transfer pair | click: Post on the pair

## Rule misses

None.

## Not touched

- Nothing skipped.
"""

NOTHING_PARKED = """# Sweep 2026-04-23

## Tie-out

- Operating Checking (1234): Bank $10,000.00 | Posted $10,000.00 | Pending 0

## Needs owner

- None. Nothing sat in the queue that the policy could not click.

## Not touched

- Nothing skipped.
"""


def _notes(tmp_path: Path, **notes: str) -> Path:
    d = tmp_path / "notes"
    d.mkdir(exist_ok=True)
    for name, text in notes.items():
        (d / f"sweep-{name}.md").write_text(text, encoding="utf-8")
    return d


def _run(ledger_dir: Path, note_dir: Path, **params):
    return run(
        "demo",
        "ap",
        "sweep-cards",
        params={"note_dir": str(note_dir), **params},
        ledger_dir=ledger_dir,
    )


def _cards(ledger_dir: Path, action_type: str = SWEEP_CARD) -> list[dict]:
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        rows = ledger.conn.execute(
            "SELECT id, status, params_json FROM approval_queue WHERE action_type = ? ORDER BY id",
            (action_type,),
        ).fetchall()
    return [
        {"id": r["id"], "status": r["status"], "params": json.loads(r["params_json"])} for r in rows
    ]


# --------------------------------------------------------------------------
# The parser: a bullet's identity comes from the bank's facts.
# --------------------------------------------------------------------------


def test_parse_reads_the_parked_section_only():
    rows = sweep_note.parse_note(TWO_PARKED)
    assert len(rows) == 2, "the matched and not-touched bullets are not questions"
    first, second = rows
    assert first.date == "2026-04-15"
    assert first.amount_cents == 137144
    assert first.direction == "out"
    assert first.bank_text == "ELECTRONIC IMAGE"
    assert first.check_ref == "3027"
    assert second.bank_text == "CARD SERVICES AUTOPAY"
    assert second.amount_cents == 295443
    assert second.check_ref == ""


def test_the_line_id_survives_a_rewording_of_the_same_feed_line():
    """A model writes the note, so next week's prose differs. The card must
    not: same date, amount, direction and bank text is the same question."""
    first = sweep_note.parse_note(TWO_PARKED)[0]
    reworded = sweep_note.parse_note(
        "## Needs owner\n\n"
        "- STILL WAITING from last week: 04/15/2026 | Operating Checking | "
        "ELECTRONIC IMAGE | $1,371.44 spent | check 3027 | click: Find match\n"
    )[0]
    assert reworded.line_id == first.line_id
    assert len(first.line_id) == 16


def test_bank_text_keeps_a_common_word_inside_the_name():
    """Found on a live note: an issuer's name carries a word that also shows
    up as emphasis ("ONE"). Emphasis is trimmed from the ENDS of a run of
    capitals and never from inside it, or the card names a payee nobody has
    heard of."""
    rows = sweep_note.parse_note(
        "## Needs owner\n\n"
        "- 04/15/2026 | Card Account | CARD ONE AUTOPAY payment | $2,954.43 received | "
        "the card's monthly autopay | click: Post on the pair\n"
    )
    assert rows[0].bank_text == "CARD ONE AUTOPAY"
    assert rows[0].direction == "in"


def test_two_different_lines_never_share_an_id():
    rows = sweep_note.parse_note(TWO_PARKED)
    assert rows[0].line_id != rows[1].line_id


def test_a_section_that_parks_nothing_yields_nothing():
    assert sweep_note.parse_note(NOTHING_PARKED) == []


def test_a_note_with_no_parked_section_yields_nothing():
    assert sweep_note.parse_note("# Sweep 2026-04-16\n\n## Tie-out\n\n- fine\n") == []


def test_a_bare_month_day_takes_its_year_from_the_note():
    """The sweep writes 09/15 as often as 09/15/2026; the year is the note's."""
    rows = sweep_note.parse_note(
        "# Sweep 2026-04-16\n\n## Needs owner\n\n- 04/15 WIRE FEE $35.00 spent | ask the bank\n",
        note_date_iso="2026-04-16",
    )
    assert rows[0].date == "2026-04-15"


# --------------------------------------------------------------------------
# The job: two parked rows, two cards, no duplicates, decided answers stick.
# --------------------------------------------------------------------------


def test_two_parked_rows_park_two_cards_keyed_by_feed_line(tmp_path):
    d = tmp_path / "d"
    notes = _notes(tmp_path, **{"2026-04-16": TWO_PARKED})

    result = _run(d, notes)
    assert result.status in ("ok", "needs_approval")

    parked = _cards(d)
    assert len(parked) == 2, "one card per parked feed line"
    ids = [c["params"]["feed_line_id"] for c in parked]
    assert ids == [r.line_id for r in sweep_note.parse_note(TWO_PARKED)]
    assert parked[0]["params"]["amount_cents"] == 137144
    assert parked[0]["params"]["check_ref"] == "3027"
    assert parked[0]["params"]["account"] == "", "the coding is the owner's to name"


def test_a_re_run_over_the_same_note_creates_no_duplicates(tmp_path):
    d = tmp_path / "d"
    notes = _notes(tmp_path, **{"2026-04-16": TWO_PARKED})
    _run(d, notes)
    _run(d, notes)
    # A fresh run key (next week's note, same two rows still waiting) must
    # still not double-ask: the key is the feed line, not the note.
    _notes(tmp_path, **{"2026-04-23": TWO_PARKED.replace("Sweep 2026-04-16", "Sweep 2026-04-23")})
    _run(d, notes)
    assert len(_cards(d)) == 2


def test_approving_with_an_account_records_the_coding(tmp_path):
    d = tmp_path / "d"
    notes = _notes(tmp_path, **{"2026-04-16": TWO_PARKED})
    _run(d, notes)
    card = _cards(d)[0]

    from core.agents.ap.jobs import check_qbo_sweep_parked

    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        try:
            ledger.resolve_approval(
                "demo",
                card["id"],
                "approved",
                check=lambda agent, action, params: check_qbo_sweep_parked(ledger, "demo", params),
            )
            raise AssertionError("an approval that names no coding must be refused")
        except ValueError as exc:
            assert "account" in str(exc)
        ledger.resolve_approval(
            "demo",
            card["id"],
            "approved",
            param_overrides={"account": "Project Expense - 1017"},
            check=lambda agent, action, params: check_qbo_sweep_parked(ledger, "demo", params),
        )

    answered = [c for c in _cards(d) if c["id"] == card["id"]][0]
    assert answered["status"] == "approved"
    assert answered["params"]["account"] == "Project Expense - 1017"


def test_a_decided_card_never_re_asks(tmp_path):
    """Approved or rejected, the owner answered. Next week's note carries the
    same bullet ('still waiting'); it is not a new question."""
    d = tmp_path / "d"
    notes = _notes(tmp_path, **{"2026-04-16": TWO_PARKED})
    _run(d, notes)
    root = resolve_ledger_root("demo", d)
    with Ledger.open(root) as ledger:
        for card in _cards(d):
            ledger.conn.execute(
                "UPDATE approval_queue SET status = 'rejected' WHERE id = ?", (card["id"],)
            )
        ledger.conn.commit()

    _notes(tmp_path, **{"2026-04-23": TWO_PARKED.replace("2026-04-16", "2026-04-23")})
    result = _run(d, notes)

    assert len(_cards(d)) == 2, "a decided card must not re-park"
    assert not any(a.code == "engine.approval_swallowed" for a in result.anomalies)


def test_a_check_the_hand_check_lane_already_asked_about_does_not_card_again(tmp_path):
    """The same physical check reaches the owner through the accounting feed,
    the bank statement, and now the sweep note. He answers it once."""
    d = tmp_path / "d"
    notes = _notes(tmp_path, **{"2026-04-16": TWO_PARKED})
    root = resolve_ledger_root("demo", d)
    _run(d, tmp_path / "no-notes-yet")  # a run with nothing to read, so the ledger exists
    with Ledger.open(root) as ledger:
        run_id = ledger.conn.execute("SELECT id FROM runs ORDER BY id LIMIT 1").fetchone()["id"]
        ledger.enqueue_approval(
            idempotency_key="apr:demo:ap:ap.record_direct_payment:direct_payment:Statement:1",
            run_id=run_id,
            tenant="demo",
            agent="ap",
            action_type=DP_CARD,
            params={"qbo_id": "Statement:1", "check_ref": "3027", "amount_cents": 137144},
        )
        ledger.conn.commit()

    _run(d, notes)

    parked = _cards(d)
    assert len(parked) == 1, "the check already in front of the owner does not card twice"
    assert parked[0]["params"]["bank_text"] == "CARD SERVICES AUTOPAY"


def test_a_hand_renamed_note_never_outranks_a_dated_one(tmp_path):
    """Folders collect strays. A dated note is the queue; a file somebody
    renamed sorts after every date and would otherwise win."""
    d = tmp_path / "d"
    notes = _notes(tmp_path, **{"2026-04-16": TWO_PARKED, "old-copy": NOTHING_PARKED})
    _run(d, notes)
    assert len(_cards(d)) == 2


def test_the_newest_note_is_the_queue(tmp_path):
    """One note per sweep and the newest is this week's queue. Older notes
    are history: their parked rows were answered or are re-listed today."""
    d = tmp_path / "d"
    notes = _notes(tmp_path, **{"2026-04-16": TWO_PARKED, "2026-04-23": NOTHING_PARKED})
    _run(d, notes)
    assert _cards(d) == []


def test_an_unreadable_note_folder_is_an_anomaly_never_a_silence(tmp_path):
    """'I could not look' is never 'nothing to look at' (the 2026-09-13
    lesson): the folder lives on a tree the operating system gates per
    program, and a silent empty listing reads exactly like a quiet week."""
    d = tmp_path / "d"
    missing = tmp_path / "gone"
    missing.mkdir()
    with unreadable(missing, 0o000):
        result = _run(d, missing)
    assert any(a.code == "qbo.sweep.note_unreadable" for a in result.anomalies)


def test_no_note_folder_is_a_quiet_skip(tmp_path):
    d = tmp_path / "d"
    result = _run(d, tmp_path / "never-existed")
    assert result.status == "ok"
    assert result.anomalies == []
    assert _cards(d) == []


def test_the_lane_can_be_switched_off(tmp_path):
    d = tmp_path / "d"
    notes = _notes(tmp_path, **{"2026-04-16": TWO_PARKED})
    result = _run(d, notes, parked_cards="false")
    assert _cards(d) == []
    assert "off" in result.summary


def test_shadow_parks_nothing_and_says_what_it_would(tmp_path):
    d = tmp_path / "d"
    notes = _notes(tmp_path, **{"2026-04-16": TWO_PARKED})
    result = run(
        "demo",
        "ap",
        "sweep-cards",
        params={"note_dir": str(notes)},
        shadow=True,
        ledger_dir=d,
    )
    assert _cards(d) == []
    assert any("would park" in a for a in result.actions)
