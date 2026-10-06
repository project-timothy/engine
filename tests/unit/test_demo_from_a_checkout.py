"""The demo tenant works from a plain checkout (issue #3, 2026-10-06).

The container runs ``engine init`` at first boot; a checkout following the
README had no data tree, so ``engine doctor demo`` printed 16 MISSING lines
and ``engine run demo ap intake`` died on the missing inbox. The README's
path is ``engine doctor demo --create-folders``; this walks it on the
shipped demo tenant, from an empty working directory.
"""

from __future__ import annotations

from core.engine.cli import main


def test_the_readme_path_leaves_no_missing_folder_and_intake_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / ".ledger"))
    assert main(["run", "demo", "demo", "ingest"]) == 0

    main(["doctor", "demo", "--create-folders"])
    capsys.readouterr()
    main(["doctor", "demo"])
    assert "MISSING  folder" not in capsys.readouterr().out

    assert main(["run", "demo", "ap", "intake"]) == 0
