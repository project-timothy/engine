"""mail/fetch: the engine fetches its own mail, filtered, private, idempotent.

2026-07-16 origin incident: the unfiltered legacy attachment feed put the
owner's MEDICAL documents into the business landing folder, and their
filenames into a ledger event log that pushes to a remote git host. This
agent replaces that feed. Two promises dominate these evals:

1. A denied sender's mail leaves NO trace in any recorded payload — not a
   filename, not an address, not a subject. Aggregate counts only.
2. Nothing is ever lost silently: allowed attachments land in the folder
   (never overwriting), and every skip is visible as a count.

Fixture messages arrive via ``--param messages_file`` so no eval touches
the network or the keychain; the live client has its own unit tests.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
from pathlib import Path

from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger


def _msg(msg_id, sender, *atts, date="2026-07-15T12:00:00Z"):
    return {
        "id": msg_id,
        "sender": sender,
        "date": date,
        "attachments": [
            {"id": f"a-{i}", "name": name, "content_b64": base64.b64encode(body).decode()}
            for i, (name, body) in enumerate(atts)
        ],
    }


def _messages_file(tmp_path: Path, *messages) -> Path:
    p = tmp_path / "messages.json"
    p.write_text(json.dumps(list(messages)))
    return p


def _run(tmp_path: Path, messages: Path, *, shadow: bool = False, denied: str = "clinic.example"):
    return run(
        "demo",
        "mail",
        "fetch",
        shadow=shadow,
        params={
            "messages_file": str(messages),
            "landing_dir": str(tmp_path / "landing"),
            "denied_senders": denied,
        },
        ledger_dir=tmp_path / "data",
    )


def _all_recorded_text(tmp_path: Path) -> str:
    """Everything the run persisted: event log + run results. The privacy
    promise is that a denied sender's identifiers appear in NONE of it."""
    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        events = json.dumps(ledger.read_event_log())
        runs = json.dumps(
            [dict(r) for r in ledger.conn.execute("SELECT result_json FROM runs").fetchall()]
        )
    return events + runs


def test_allowed_attachment_lands_in_the_folder_with_provenance(tmp_path):
    messages = _messages_file(
        tmp_path, _msg("m1", "billing@acmetooling.com", ("invoice-4471.pdf", b"pdf-bytes"))
    )

    result = _run(tmp_path, messages)

    assert result.status == "ok"
    saved = tmp_path / "landing" / "invoice-4471.pdf"
    assert saved.read_bytes() == b"pdf-bytes"
    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        (ev,) = [e for e in ledger.read_event_log() if e["event_type"] == "mail.attachment_saved"]
    assert ev["payload"]["file"] == "invoice-4471.pdf"
    assert ev["payload"]["sender_domain"] == "acmetooling.com"


def test_denied_sender_leaves_no_identifying_trace_anywhere(tmp_path):
    messages = _messages_file(
        tmp_path,
        _msg("m1", "schedule@clinic.example", ("D. Meer Care Plan.pdf", b"phi-bytes")),
        _msg("m2", "billing@acmetooling.com", ("invoice-1.pdf", b"pdf")),
    )

    result = _run(tmp_path, messages)

    assert result.status == "ok"
    assert not (tmp_path / "landing" / "D. Meer Care Plan.pdf").exists()
    assert "denied: 1" in result.summary  # visible as a count...
    recorded = _all_recorded_text(tmp_path)
    for token in ("clinic.example", "Care Plan", "schedule@"):
        assert token not in recorded  # ...and as nothing else, anywhere


def test_extension_filter_skips_but_counts(tmp_path):
    messages = _messages_file(
        tmp_path,
        _msg("m1", "it@acmetooling.com", ("installer.exe", b"MZ"), ("invoice.pdf", b"pdf")),
    )

    result = _run(tmp_path, messages)

    assert (tmp_path / "landing" / "invoice.pdf").exists()
    assert not (tmp_path / "landing" / "installer.exe").exists()
    assert "filtered: 1" in result.summary


def test_resend_of_identical_content_is_adopted_not_duplicated(tmp_path):
    m1 = _messages_file(tmp_path, _msg("m1", "billing@acmetooling.com", ("inv.pdf", b"same")))
    _run(tmp_path, m1)
    m2 = _messages_file(tmp_path, _msg("m2", "billing@acmetooling.com", ("inv.pdf", b"same")))

    result = _run(tmp_path, m2)

    assert result.status in ("ok", "noop")
    landing = tmp_path / "landing"
    assert [p.name for p in landing.iterdir()] == ["inv.pdf"]  # one copy, no suffixes


def test_name_collision_with_different_content_gets_a_suffix(tmp_path):
    m1 = _messages_file(tmp_path, _msg("m1", "billing@acmetooling.com", ("inv.pdf", b"one")))
    _run(tmp_path, m1)
    m2 = _messages_file(tmp_path, _msg("m2", "ar@betafreight.com", ("inv.pdf", b"two")))

    _run(tmp_path, m2)

    names = sorted(p.name for p in (tmp_path / "landing").iterdir())
    assert names == ["inv (2).pdf", "inv.pdf"]
    assert (tmp_path / "landing" / "inv.pdf").read_bytes() == b"one"  # original untouched


def test_rerun_with_same_mailbox_state_is_a_noop(tmp_path):
    messages = _messages_file(tmp_path, _msg("m1", "billing@acmetooling.com", ("inv.pdf", b"pdf")))
    first = _run(tmp_path, messages)
    again = _run(tmp_path, messages)

    assert first.status == "ok"
    assert again.status == "noop"


def test_shadow_saves_nothing_and_reports_what_it_would_do(tmp_path):
    messages = _messages_file(tmp_path, _msg("m1", "billing@acmetooling.com", ("inv.pdf", b"pdf")))

    result = _run(tmp_path, messages, shadow=True)

    assert result.status == "ok"
    assert not (tmp_path / "landing").exists()
    assert any("would save" in a for a in result.actions)


# ---- cc-charge routing (expenses design decision 3, docs/expenses-design.md) --------------


def _run_cc(tmp_path: Path, messages: Path, *, shadow: bool = False):
    return run(
        "demo",
        "mail",
        "fetch",
        shadow=shadow,
        params={
            "messages_file": str(messages),
            "landing_dir": str(tmp_path / "landing"),
            "denied_senders": "clinic.example",
            "cc_charge_senders": "cardcharges.example",
            "expenses_filing_dir": str(tmp_path / "expenses"),
        },
        ledger_dir=tmp_path / "data",
    )


def test_cc_charge_receipt_files_to_cc_charges_not_landing(tmp_path):
    messages = _messages_file(
        tmp_path,
        _msg("m1", "receipts@cardcharges.example", ("api-receipt.pdf", b"cc-bytes")),
        _msg("m2", "billing@vendor.example", ("invoice.pdf", b"inv-bytes")),
    )

    result = _run_cc(tmp_path, messages)

    # the cc receipt is filed paper in the month's _cc_charges, with a card note
    filed = tmp_path / "expenses" / "2026-07" / "_cc_charges" / "api-receipt.pdf"
    assert filed.is_file()
    assert not (tmp_path / "landing" / "api-receipt.pdf").exists()
    (card,) = result.approvals_needed
    assert card.action_type == "expenses.cc_charge_filed"
    # the normal invoice still lands normally
    assert (tmp_path / "landing" / "invoice.pdf").is_file()
    # and the not-landed lens sees only the invoice: cc paper is a different
    # event type, so it can never read as a not-landed invoice
    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        by_type = {}
        for e in ledger.read_event_log():
            by_type.setdefault(e["event_type"], []).append(e["payload"].get("file"))
    assert by_type.get("mail.attachment_saved") == ["invoice.pdf"]
    assert by_type.get("mail.cc_charge_filed") == ["api-receipt.pdf"]


def _tenant_without_an_expenses_tree(tmp_path, monkeypatch) -> None:
    """Point the engine at a copy of the demo whose ``[expenses].filing_dir``
    is blank.

    Since row 7.19 the demo tenant is rendered from the archetype template
    and DOES name an expenses tree, so a tenant without one has to be built
    rather than borrowed; blanking the one key leaves every other value
    identical, and the slug stays ``demo`` so the ledger is unchanged.
    """
    root = tmp_path / "tenants-no-expenses"
    (root / "demo").mkdir(parents=True, exist_ok=True)
    demo_dir = Path(__file__).resolve().parents[4] / "tenants" / "demo"
    text = (demo_dir / "tenant.toml").read_text(encoding="utf-8")
    blanked, count = re.subn(r'(?m)^filing_dir = "[^"]*/expenses/filed"$', 'filing_dir = ""', text)
    assert count == 1, "[expenses].filing_dir is the only key that names it"
    (root / "demo" / "tenant.toml").write_text(blanked, encoding="utf-8")
    shutil.copy2(demo_dir / "vendors.toml", root / "demo" / "vendors.toml")
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))


def test_cc_route_dedups_across_runs_and_falls_back_without_expenses_dir(tmp_path, monkeypatch):
    messages = _messages_file(
        tmp_path,
        _msg("m1", "receipts@cardcharges.example", ("api-receipt.pdf", b"cc-bytes")),
    )
    _run_cc(tmp_path, messages)

    again = _messages_file(
        tmp_path,
        _msg("m9", "receipts@cardcharges.example", ("api-receipt.pdf", b"cc-bytes")),
    )
    result = _run_cc(tmp_path, again)
    assert "dup: 1" in result.summary

    # a tenant with no expenses tree keeps the old behavior: normal landing
    plain = _messages_file(
        tmp_path, _msg("m3", "receipts@cardcharges.example", ("other.pdf", b"x"))
    )
    _tenant_without_an_expenses_tree(tmp_path, monkeypatch)
    result = run(
        "demo",
        "mail",
        "fetch",
        params={
            "messages_file": str(plain),
            "landing_dir": str(tmp_path / "landing2"),
            "denied_senders": "clinic.example",
            "cc_charge_senders": "cardcharges.example",
        },
        ledger_dir=tmp_path / "data2",
    )
    assert (tmp_path / "landing2" / "other.pdf").is_file()


def test_save_that_reads_back_wrong_is_not_recorded_and_retries(tmp_path, monkeypatch):
    """Honesty audit 2026-09-03, 03-F4 (S2). "saved N" stood on write_bytes
    returning. Now the landing file is read back; a mismatch is an anomaly
    with no event, so the message is not "seen" and the next fetch retries."""
    from core.engine import fileops

    m1 = _messages_file(tmp_path, _msg("m1", "billing@acmetooling.com", ("inv.pdf", b"real")))
    real_write = Path.write_bytes

    def _short_write(self, data):
        return real_write(self, b"re")  # a truncated write on a sync mount

    monkeypatch.setattr(fileops.Path, "write_bytes", _short_write)
    result = _run(tmp_path, m1)

    assert any(a.code == "mail.save_unverified" for a in result.anomalies)
    assert not (tmp_path / "landing" / "inv.pdf").exists()  # partial removed
    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        assert [
            e for e in ledger.read_event_log() if e["event_type"] == "mail.attachment_saved"
        ] == []

    # The unsaved attachment is not "seen", so the next run that re-fires
    # (new mail) saves it; the failed save left nothing behind to adopt.
    monkeypatch.setattr(fileops.Path, "write_bytes", real_write)
    m2 = _messages_file(
        tmp_path,
        _msg("m1", "billing@acmetooling.com", ("inv.pdf", b"real")),
        _msg("m2", "ar@betafreight.com", ("other.pdf", b"other")),
    )
    again = _run(tmp_path, m2)

    assert again.status == "ok"
    assert (tmp_path / "landing" / "inv.pdf").read_bytes() == b"real"
    assert (tmp_path / "landing" / "other.pdf").read_bytes() == b"other"
