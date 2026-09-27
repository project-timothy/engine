"""Unit tests for the incident-to-eval converter (learning loop v1)."""

from __future__ import annotations

import pytest

from core.learning.incident_to_eval import (
    eval_test_name,
    parse_incident,
    render_eval_stub,
)

SAMPLE = """\
## 2026-05-21 — Payment verified against cleared transactions only

**Pattern.** A payment run treated "has not cleared the bank register" as
"has not been paid". Scheduled bill-pay items read as payable.

**Bad pattern.** Checking only the last of three payment stages.

**Fix.** Verification before any payment run is three-way: ledger, cleared
register, and bill-pay queue.

**Enforcement.** Engine code, not agent judgment.
"""


def test_parse_incident_extracts_fields():
    incident = parse_incident(SAMPLE)
    assert incident.date == "2026-05-21"
    assert incident.title.startswith("Payment verified")
    assert "three payment stages" in incident.bad_pattern
    assert "three-way" in incident.fix


def test_parse_incident_without_header_raises():
    with pytest.raises(ValueError):
        parse_incident("no header here, just prose")


def test_render_stub_is_valid_skipped_python():
    incident = parse_incident(SAMPLE)
    src = render_eval_stub(incident, module_hint="core/agents/ap/verify.py")
    # Renders to valid Python.
    compile(src, "<stub>", "exec")
    # Carries the skip reason that names the Phase 2 module to unskip.
    assert 'reason="Phase 2: core/agents/ap/verify.py"' in src
    assert "@pytest.mark.phase2" in src
    assert eval_test_name(incident) in src
    assert "2026-05-21" in src
