"""``docs/components.md`` lists the helpers under ``core/`` (2026-10-11).

"Reuse once it's proven": before writing a helper, look for one that
already does the job. The list is generated from the code, so a stale file
fails here; regenerate with ``uv run python -m core.evals.components``.
"""

from __future__ import annotations

from pathlib import Path

from core.evals import components

REPO = Path(__file__).resolve().parents[2]


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_the_committed_list_is_current():
    assert (REPO / "docs" / "components.md").read_text() == components.render(REPO), (
        "docs/components.md is stale: uv run python -m core.evals.components"
    )


def test_a_public_documented_function_is_one_line(tmp_path):
    root = _tree(
        tmp_path,
        {
            "core/money/round.py": (
                'def cents(x):\n    """Round a decimal to whole cents.\n\n'
                '    More detail.\n    """\n'
            ),
        },
    )
    text = components.render(root)
    assert "| `core.money.round` | `cents` | Round a decimal to whole cents. |" in text


def test_private_undocumented_evals_and_entry_points_are_left_out(tmp_path):
    root = _tree(
        tmp_path,
        {
            "core/a/helpers.py": (
                'def _hidden():\n    """Private."""\n\n'
                "def bare():\n    pass\n\n"
                'class Thing:\n    """A class, not a helper."""\n'
            ),
            "core/a/evals/test_x.py": 'def make_fixture():\n    """Eval helper."""\n',
            "core/a/jobs.py": 'def handler():\n    """A job, not a helper."""\n',
            "core/engine/cli.py": 'def main():\n    """The CLI."""\n',
        },
    )
    text = components.render(root)
    for name in ("_hidden", "bare", "Thing", "make_fixture", "handler", "`main`"):
        assert name not in text


def test_a_multiline_first_sentence_is_joined_and_pipes_escaped(tmp_path):
    root = _tree(
        tmp_path,
        {
            "core/b.py": (
                'def pick(a):\n    """Pick a | b from the list,\n    whichever is first.\n\n'
                '    Detail.\n    """\n'
            ),
        },
    )
    assert "Pick a \\| b from the list, whichever is first." in components.render(root)


def test_main_writes_the_file(tmp_path):
    root = _tree(tmp_path, {"core/c.py": 'def go():\n    """Go."""\n', "docs/.keep": ""})
    assert components.main(["--root", str(root)]) == 0
    assert (root / "docs" / "components.md").read_text() == components.render(root)
