"""A charity that files a Form 990 starts with the filing on its calendar
(the alpha dress rehearsal, 2026-10-15): asked "what's due this year?", Tim
had nothing to say, though setup had heard the answer. The 990 is due the
15th day of the fifth month after the fiscal year ends, every year. A church
files none, so it gets none."""

from __future__ import annotations

from datetime import date

import pytest

from core.agents.deadlines.schema import load_obligations
from core.onboarding import apply, next_question, plan, record


def _set_up(tmp_path, entity: str, fiscal_start: str, today: date):
    root = tmp_path / "tenants"
    ledger_dir = tmp_path / "ledger"
    values = {
        "who": "nonprofit-solo",
        "legal_name": "Open Door Scholarships Inc.",
        "your_name": "Eli Porter",
        "reviewer": "Martin Porter, martin@example.org",
        "entity_nonprofit": entity,
        "fiscal_start": fiscal_start,
    }
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    apply(
        plan(answers, "open-door"), root=root, ledger_dir=ledger_dir, run_audit=False, today=today
    )
    return load_obligations(root / "open-door" / "obligations.toml")


@pytest.mark.parametrize(
    ("fiscal_start", "today", "due"),
    [
        ("January", date(2026, 10, 15), date(2027, 5, 15)),
        ("January", date(2026, 3, 1), date(2026, 5, 15)),
        ("July", date(2026, 10, 15), date(2026, 11, 15)),
    ],
)
def test_a_990_filer_starts_with_its_next_filing_on_the_calendar(
    tmp_path, fiscal_start, today, due
):
    [ob] = _set_up(tmp_path, "A charity that files a Form 990", fiscal_start, today)
    assert (ob.id, ob.kind, ob.due, ob.every) == ("form-990", "tax", due, "1y")
    assert "Form 990" in ob.title


def test_a_church_files_no_990_and_starts_with_an_empty_calendar(tmp_path):
    assert _set_up(tmp_path, "A church", "January", date(2026, 10, 15)) == []
