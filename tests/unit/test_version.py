"""An operator can name the version they run and read what changed (public #7).

``engine --version`` and ``auditor --version`` print the package version and
the commit; ``engine doctor`` prints the same line first; CHANGELOG.md has a
section for the package version and names every ledger migration, so a
schema change cannot ship without a line an upgrader will read.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from auditor.cli import main as auditor_main
from core.engine import version
from core.engine.cli import main as engine_main
from core.ledger.schema import MIGRATIONS

REPO = Path(__file__).resolve().parents[2]
PACKAGE_VERSION = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
CHANGELOG = (REPO / "CHANGELOG.md").read_text() if (REPO / "CHANGELOG.md").exists() else ""


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("main,prog", [(engine_main, "engine"), (auditor_main, "auditor")])
def test_version_flag_prints_package_version_and_commit(main, prog, capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["--version"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith(f"{prog} {PACKAGE_VERSION} (")
    head = _head()
    if head:  # a checkout; an image names itself instead (see below)
        assert head in out


def test_without_a_checkout_the_image_reference_stands_in_for_the_commit(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_IMAGE", "ghcr.io/example/engine@sha256:abc")
    assert version.describe("engine", source=tmp_path) == (
        f"engine {PACKAGE_VERSION} (image ghcr.io/example/engine@sha256:abc)"
    )


def test_without_a_checkout_or_an_image_the_commit_is_unknown(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGINE_IMAGE", raising=False)
    assert (
        version.describe("engine", source=tmp_path) == f"engine {PACKAGE_VERSION} (commit unknown)"
    )


def test_the_auditor_reads_the_same_version_without_importing_core(monkeypatch, tmp_path):
    from auditor import version as auditor_version

    monkeypatch.delenv("ENGINE_IMAGE", raising=False)
    assert auditor_version.describe("auditor", source=tmp_path) == (
        f"auditor {PACKAGE_VERSION} (commit unknown)"
    )


def test_changelog_has_a_section_for_the_package_version():
    assert re.search(rf"^## \[?{re.escape(PACKAGE_VERSION)}\]?\b", CHANGELOG, re.M), (
        f"CHANGELOG.md needs a '## {PACKAGE_VERSION}' section"
    )


def test_changelog_names_every_ledger_migration():
    named = {int(n) for n in re.findall(r"ledger migration (\d+)", CHANGELOG)}
    shipped = {number for number, _ in MIGRATIONS}
    assert shipped - named == set(), "say what these ledger migrations change in CHANGELOG.md"
