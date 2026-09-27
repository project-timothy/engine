"""The independence boundary, tested on itself and on planted violations.

A checker that shares the worker's code shares the worker's bugs
(docs/auditor-design.md, principle 1). This eval proves both directions:
the real tree is clean, and the lint actually catches every import shape
that would breach the boundary.
"""

from __future__ import annotations

from auditor.evals.independence_lint import auditor_root, scan


def test_real_auditor_tree_is_clean():
    assert scan(auditor_root()) == []


def _plant(tmp_path, source):
    (tmp_path / "lens.py").write_text(source)
    return scan(tmp_path)


def test_catches_import_core(tmp_path):
    assert _plant(tmp_path, "import core\n")


def test_catches_from_core_import(tmp_path):
    assert _plant(tmp_path, "from core.ledger import Ledger\n")


def test_catches_import_core_submodule(tmp_path):
    assert _plant(tmp_path, "import core.agents.ap.jobs as jobs\n")


def test_catches_lazy_import_inside_function(tmp_path):
    assert _plant(tmp_path, "def f():\n    from core.engine import runner\n")


def test_catches_importlib_escape_hatch(tmp_path):
    assert _plant(tmp_path, 'import importlib\nm = importlib.import_module("core.ledger")\n')


def test_ignores_lookalike_modules(tmp_path):
    clean = "import corelib\nfrom score import thing\nfrom .core_helpers import x\n"
    assert _plant(tmp_path, clean) == []


def test_ignores_the_word_core_in_strings_and_comments(tmp_path):
    clean = '# the core principle\nMSG = "core values"\n'
    assert _plant(tmp_path, clean) == []
