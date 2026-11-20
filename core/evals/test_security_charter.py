"""The security charter gate (chunk 4, 2026-10-16): every rule has a test.

A rule in ``docs/security/charter.md`` is proven by the tests that carry its
marker, ``@pytest.mark.security("SEC-01")``. The gate fails a rule no test
carries, a marker naming no rule, and a charter whose "Proven by" line
disagrees with where the markers really are.
"""

from __future__ import annotations

from pathlib import Path

from core.evals import security_charter as gate

CHARTER = """# Charter

### SEC-01: Money moves only through a card

Prose.

Proven by: tests/test_a.py

### SEC-02: No agent releases money

Proven by: tests/test_a.py, tests/test_b.py
"""

TEST_A = """import pytest

@pytest.mark.security("SEC-01")
def test_one():
    pass

@pytest.mark.security("SEC-02")
def test_two():
    pass
"""

TEST_B = """import pytest

class TestGroup:
    @pytest.mark.security("SEC-02")
    def test_three(self):
        pass
"""


def _repo(tmp_path: Path, charter: str = CHARTER, **tests: str) -> Path:
    (tmp_path / "docs" / "security").mkdir(parents=True)
    (tmp_path / "docs" / "security" / "charter.md").write_text(charter)
    (tmp_path / "tests").mkdir()
    for name, body in ({"test_a": TEST_A, "test_b": TEST_B} | tests).items():
        if body:
            (tmp_path / "tests" / f"{name}.py").write_text(body)
    return tmp_path


def test_the_real_charter_holds():
    assert gate.check() == []


def test_the_real_charter_names_rules():
    assert len(gate.rules(gate.repo_root())) >= 12


def test_a_charter_whose_every_rule_is_proven_passes(tmp_path):
    assert gate.check(_repo(tmp_path)) == []


def test_a_rule_no_test_carries_fails(tmp_path):
    root = _repo(tmp_path, test_b="", test_a=TEST_A.replace('"SEC-02"', '"SEC-01"'))
    assert any("SEC-02" in p and "no test" in p for p in gate.check(root))


def test_a_marker_naming_no_rule_fails(tmp_path):
    root = _repo(
        tmp_path,
        test_c='import pytest\n\n@pytest.mark.security("SEC-09")\ndef test_x():\n    pass\n',
    )
    assert any("SEC-09" in p for p in gate.check(root))


def test_a_marker_with_no_rule_named_fails(tmp_path):
    root = _repo(
        tmp_path, test_c="import pytest\n\n@pytest.mark.security()\ndef test_x():\n    pass\n"
    )
    assert any("names no rule" in p for p in gate.check(root))


def test_a_proven_by_line_that_disagrees_with_the_markers_fails(tmp_path):
    root = _repo(
        tmp_path, charter=CHARTER.replace("tests/test_a.py, tests/test_b.py", "tests/test_a.py")
    )
    assert any("SEC-02" in p and "tests/test_b.py" in p for p in gate.check(root))


def test_a_rule_with_no_proven_by_line_fails(tmp_path):
    root = _repo(tmp_path, charter=CHARTER.replace("Proven by: tests/test_a.py\n", "", 1))
    assert any("SEC-01" in p and "Proven by" in p for p in gate.check(root))


def test_one_marker_may_prove_two_rules(tmp_path):
    both = 'import pytest\n\n@pytest.mark.security("SEC-01", "SEC-02")\ndef test_x():\n    pass\n'
    charter = CHARTER.replace("tests/test_a.py, tests/test_b.py", "tests/test_b.py").replace(
        "Proven by: tests/test_a.py\n", "Proven by: tests/test_b.py\n"
    )
    assert gate.check(_repo(tmp_path, charter=charter, test_a="", test_b=both)) == []


def test_the_rule_id_in_a_string_or_comment_is_no_marker(tmp_path):
    words = '# @pytest.mark.security("SEC-09")\nX = \'pytest.mark.security("SEC-09")\'\n'
    assert gate.check(_repo(tmp_path, test_c=words)) == []
