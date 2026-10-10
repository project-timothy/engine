"""A tenant with no authority.toml behaves byte-identically (#435).

``fixtures/authority_equivalence.json`` was produced by the harness
(``authority_equivalence.py``) on main before #435 (6d2d940): the queue
CLI, the brief's send and the calendar write, run with the first tenant's
shape of config (no authority.toml, no kit, no shape). The code as it stands
must leave exactly the same cards, events, output and run results.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.authority_equivalence import dump

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "authority_equivalence.json"


def test_a_tenant_without_authority_toml_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(tmp_path))  # dump() sets its own; restored after
    now = json.loads(json.dumps(dump(tmp_path / "eq", keys=False), sort_keys=True))
    assert now == json.loads(GOLDEN.read_text(encoding="utf-8"))
