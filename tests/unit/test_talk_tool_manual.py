"""What Tim's own loop sends about its tools costs less (Standalone Tim shot 2,
card C1, 2026-11-17).

Tim runs his own model at a few dollars a month per ministry, and the loop
sends the person's whole tool list on every turn. A tool may carry a short
form for the loop (``ToolSpec.short``): what it does, when to call it, and
what its arguments mean, leaving out what the brief (core/talk/brief.md) and
the hold (core/talk/hold.py) already say for every tool. The door's own
words stay as they are (tests/unit/test_door_words_pinned.py).

Measured on the made-up ministry the talk tests use, as the box's door
builds a person's tools: the system text for a solo missionary was 43,260
characters (about 10,815 tokens at 4 characters a token) and for her
reviewer 42,323 (about 10,580) before the short forms; after, 35,138
(about 8,784) and 34,311 (about 8,577), 19 percent less. The tools' input
schemas, about 14,000 characters, are sent as the door serves them. The
ceilings below hold the after-size, so it only goes down.

Raised on 2026-11-18, by exactly the 379 characters the brief gained from
the first live trial (issue #681: read the books again before repeating an
amount, and no em dash), and again that day by exactly the 329 it gained
from the second (the waiting item only when asked, and a tax question goes
to the packet first). The tool list itself did not grow. Lowered that
evening to the new size, when a call waiting for her tap stopped ending
Tim's turn: the note it sends back says what to do, so the brief's line
about it went. Lowered again when waiting_cards took a short form that says
whose yes each card is (the Haiku run, A09), 28 characters under the door's.
Lowered again when decide_card took its third decision, take_back (the
owner's call of 2026-11-18, A06), with a shorter short form.
Raised on 2026-11-20 by exactly the 248 characters of the brief's line that
money owed back either way is a repayment, never income or a deduction (the
Haiku year's S07: Tim sent that question to her preparer).
"""

from __future__ import annotations

from core.talk.loop import _system
from core.tools import catalog
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_talk_yes import box, every_spec

CEILING = {"daniel-reyes": 36_438, "linda-park": 35_611}
"""Characters of the loop's system text, the solo missionary and her reviewer.
2026-11-20: +173 each for the brief's `books_now` line (he stands by an
answer unless a fresh read disagrees); +120 each for the `who_approves` line;
+140 each for paid_it_myself's `again`."""


def system_text(where, person: str) -> str:
    tools = box(where, person)
    return _system(tools, {s.name: s for s in tools.specs()})


def test_the_loop_reads_a_tools_short_form_when_it_has_one():
    def fn(t, a):
        return {}

    long = catalog.ToolSpec("long_one", "Every word the door serves.", {}, fn)
    short = catalog.ToolSpec("short_one", "Every word the door serves.", {}, fn, short="Fewer.")
    text = _system(box_less(), {"long_one": long, "short_one": short})
    assert "- long_one: Every word the door serves." in text
    assert "- short_one: Fewer." in text
    assert "short_one: Every word" not in text


def box_less():
    """The least a system text needs to say who is speaking."""
    return catalog.Tools("living-water", ledger_root=__import__("pathlib").Path("/nonexistent"))


def test_a_short_form_is_shorter_than_the_door_words_it_stands_for():
    for name, spec in every_spec().items():
        if spec.short:
            assert len(spec.short) < len(spec.description), name
            assert "—" not in spec.short, name


def test_the_loops_tool_manual_stays_under_its_ceiling(solo):  # noqa: F811
    for person, ceiling in CEILING.items():
        size = len(system_text(solo, person))
        assert size <= ceiling, f"{person}: {size} characters, ceiling {ceiling}"
