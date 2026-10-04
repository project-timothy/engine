"""One ledger, one run: concurrent runs on the same tenant ledger refuse.

The 2026-08-20 assessment: a launchd run and an interactive run on the same
ledger could both pass find_run and double-execute; worse, the ledger's
git commit sweeps the OTHER process's mid-run writes into a mis-attributed
commit (commit_all stages everything). A per-ledger flock closes it: the
second run returns a structured refusal (never recorded, so a later re-run
executes normally), and the lock dies with the process — no stale-lock
janitor needed.

Honesty audit 2026-09-03, substrate finding F3: the CLI's own ledger writes
(``queue approve|reject``, ``status``) commit the ledger too, and they used
to do it OUTSIDE the lock, so an approval taken mid-run swept the running
job's rows into the approval's commit. They now take the same lock and, on
contention, refuse with the same structured shape and exit 2.
"""

from __future__ import annotations

import fcntl
from pathlib import Path

from core.engine.cli import main
from core.engine.runner import resolve_ledger_root, run

LANDING = Path(__file__).resolve().parents[2] / "core/agents/ap/evals/fixtures/landing"


def _run(tmp_path):
    return run(
        "demo",
        "ap",
        "queue-status",
        shadow=True,
        params={},
        ledger_dir=tmp_path,
    )


def test_contended_ledger_refuses_with_a_structured_result(tmp_path):
    root = resolve_ledger_root("demo", tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    holder = open(root / ".engine-run.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _run(tmp_path)
    finally:
        holder.close()

    assert result.status == "error"
    assert any(a.code == "engine.run_locked" for a in result.anomalies)


def test_lock_release_lets_the_next_run_execute(tmp_path):
    root = resolve_ledger_root("demo", tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    holder = open(root / ".engine-run.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    refused = _run(tmp_path)
    holder.close()  # releases the flock

    after = _run(tmp_path)

    assert refused.status == "error"
    assert after.status in ("ok", "noop", "needs_approval")


def test_uncontended_run_is_unaffected(tmp_path):
    result = _run(tmp_path)
    assert result.status in ("ok", "noop", "needs_approval")


def _seed_cards(tmp_path):
    code = main(
        [
            "run",
            "demo",
            "ap",
            "intake",
            "--shadow",
            "--ledger-dir",
            str(tmp_path),
            "--param",
            f"landing_dir={LANDING}",
            "--param",
            "extractor=fixture",
        ]
    )
    assert code == 0


def _pending_ids(tmp_path, capsys):
    capsys.readouterr()
    main(["queue", "list", "demo", "--ledger-dir", str(tmp_path), "--status", "pending"])
    out = capsys.readouterr().out
    return [int(line.split()[0].lstrip("#")) for line in out.splitlines() if line.startswith("#")]


def _hold(tmp_path):
    root = resolve_ledger_root("demo", tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    holder = open(root / ".engine-run.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return holder


def test_queue_approve_under_a_held_lock_refuses_and_leaves_the_card_pending(tmp_path, capsys):
    _seed_cards(tmp_path)
    (first_id, *_rest) = _pending_ids(tmp_path, capsys)

    holder = _hold(tmp_path)
    try:
        code = main(
            ["queue", "approve", "demo", "--id", str(first_id), "--ledger-dir", str(tmp_path)]
        )
        captured = capsys.readouterr()
    finally:
        holder.close()

    assert code == 2
    assert "engine.run_locked" in captured.err
    assert "another run holds the ledger lock" in captured.err
    assert "approved" not in captured.out
    # The card is untouched: still pending, nothing committed under its name.
    assert first_id in _pending_ids(tmp_path, capsys)

    # Once the lock is released the same approval goes through.
    code = main(["queue", "approve", "demo", "--id", str(first_id), "--ledger-dir", str(tmp_path)])
    assert code == 0
    assert first_id not in _pending_ids(tmp_path, capsys)


def test_queue_list_reads_through_a_held_lock(tmp_path, capsys):
    _seed_cards(tmp_path)
    holder = _hold(tmp_path)
    try:
        capsys.readouterr()
        code = main(["queue", "list", "demo", "--ledger-dir", str(tmp_path)])
        out = capsys.readouterr().out
    finally:
        holder.close()
    assert code == 0
    assert "[pending]" in out


def test_status_under_a_held_lock_refuses_before_touching_the_ledger(tmp_path, capsys):
    _seed_cards(tmp_path)
    holder = _hold(tmp_path)
    try:
        capsys.readouterr()
        code = main(["status", "demo", "INV-1", "--paid", "--ledger-dir", str(tmp_path)])
        captured = capsys.readouterr()
    finally:
        holder.close()
    assert code == 2
    assert "engine.run_locked" in captured.err
    assert "->" not in captured.out
