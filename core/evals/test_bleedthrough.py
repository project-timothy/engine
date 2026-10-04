"""Eval: the tenant boundary holds. Runs the bleed-through lint inside the
suite so a local ``pytest`` catches a leak even before CI does."""

from __future__ import annotations

from core.evals.bleedthrough_lint import scan


def test_no_tenant_bleed_through_under_core():
    hits = scan()
    report = "\n".join(f"  {h.path}:{h.line_number}: token {h.token!r}" for h in hits)
    assert hits == [], f"tenant tokens leaked into core/ or auditor/:\n{report}"


def test_auditor_tree_is_inside_the_scan():
    from core.evals.bleedthrough_lint import default_roots

    assert any(root.name == "auditor" for root in default_roots())


def test_the_shipped_token_list_names_no_business(tmp_path):
    """The list of what must never appear is not itself published: the
    shipped file carries path shapes only, never a name."""
    from core.evals.bleedthrough_lint import load_tokens

    shipped = load_tokens(extra="")
    assert shipped, "the shipped list keeps its path shapes"
    assert all("/" in token for token in shipped), shipped


def test_private_tokens_join_the_scan_and_catch_a_leak(tmp_path):
    from core.evals.bleedthrough_lint import load_tokens, scan

    private = tmp_path / "tokens.txt"
    private.write_text("# a tenant's names\nAcme Widgets\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "leak.py").write_text("# paid acme widgets again\n", encoding="utf-8")
    tokens = load_tokens(extra=str(private))
    assert "Acme Widgets" in tokens
    hits = scan(tree, tokens)
    assert [(h.path.name, h.token) for h in hits] == [("leak.py", "Acme Widgets")]


def test_a_named_private_list_that_is_missing_is_an_error_not_a_clean_scan(tmp_path):
    import pytest

    from core.evals.bleedthrough_lint import load_tokens

    with pytest.raises(FileNotFoundError):
        load_tokens(extra=str(tmp_path / "absent.txt"))


def test_the_wide_scan_covers_everything_an_export_ships():
    from core.evals.bleedthrough_lint import wide_roots

    names = {root.name for root in wide_roots()}
    assert {"core", "auditor", "docs", "tests", "scripts", "skills", "README.md"} <= names


def test_a_line_marked_allow_is_skipped_and_nothing_else_is(tmp_path):
    from core.evals.bleedthrough_lint import scan

    token = "/Us" + "ers/"  # spelled apart so this file is not itself a hit
    (tmp_path / "t.py").write_text(
        f'assert "{token}" not in text  # bleedthrough: allow (asserts absence)\n'
        f'HOME = "{token}someone"\n',
        encoding="utf-8",
    )
    hits = scan(tmp_path, [token])
    assert [h.line_number for h in hits] == [2]


def test_a_path_shape_matches_exactly_and_a_name_ignores_case(tmp_path):
    from core.evals.bleedthrough_lint import scan

    home = "/Us" + "ers/"
    (tmp_path / "t.py").write_text(
        'BASE = "https://api.example.com/v1/users/me"\n'
        f'HOME = "{home}someone"\n'
        "# paid ACME WIDGETS\n",
        encoding="utf-8",
    )
    hits = scan(tmp_path, [home, "Acme Widgets"])
    assert sorted(h.line_number for h in hits) == [2, 3]


def test_scripts_the_dockerfile_and_dotfiles_are_scanned_too(tmp_path):
    """2026-09-27 pre-publish read: a vendor name sat in a shell script's
    comment, and .sh was not a scanned suffix. Everything text-shaped that
    ships is in the scan, whatever its extension (or lack of one)."""
    from core.evals.bleedthrough_lint import load_tokens, scan

    private = tmp_path / "tokens.txt"
    private.write_text("Acme Widgets\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    for name in ("run.sh", "Dockerfile", ".dockerignore", ".gitignore"):
        (tree / name).write_text("# acme widgets\n", encoding="utf-8")
    hits = scan(tree, load_tokens(extra=str(private)))
    assert sorted(h.path.name for h in hits) == [
        ".dockerignore",
        ".gitignore",
        "Dockerfile",
        "run.sh",
    ]
