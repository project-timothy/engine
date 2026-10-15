"""The duplicate-code ceiling (2026-10-11): the count only goes down.

``python -m core.evals.duplicate_code`` runs pylint's duplicate-code check
over ``core/`` and fails when the count of duplicated blocks rises above
``CEILING``. A lower count passes and says to ratchet the ceiling down.
"""

from __future__ import annotations

from core.evals import duplicate_code

REPORT = """\
************* Module core.agents.ap.evals.test_reconcile
core/agents/ap/evals/test_reconcile.py:1:0: R0801: Similar lines in 2 files
==core.agents.ap.evals.test_reconcile:[35:71]
==core.agents.ap.evals.test_reconcile_review_execute:[46:81]
        ...
core/agents/close/packet.py:1:0: R0801: Similar lines in 2 files
==core.agents.close.packet:[190:201]
==core.agents.close.statements:[179:190]
        ...

-----------------------------------
Your code has been rated at 9.99/10
"""


def test_count_reads_each_duplicated_block():
    assert duplicate_code.count(REPORT) == 2
    assert duplicate_code.count("Your code has been rated at 10.00/10\n") == 0


def test_a_count_above_the_ceiling_fails(capsys):
    assert duplicate_code.verdict(17, ceiling=16) == 1
    assert "rose to 17" in capsys.readouterr().out


def test_the_ceiling_passes(capsys):
    assert duplicate_code.verdict(16, ceiling=16) == 0
    assert "16 duplicated blocks" in capsys.readouterr().out


def test_a_lower_count_passes_and_asks_for_the_ratchet(capsys):
    assert duplicate_code.verdict(12, ceiling=16) == 0
    assert "lower CEILING to 12" in capsys.readouterr().out


def test_the_command_pins_the_settings():
    cmd = duplicate_code.command()
    assert "--enable=duplicate-code" in cmd
    assert "--min-similarity-lines=8" in cmd
    assert cmd[-1] == "core"
