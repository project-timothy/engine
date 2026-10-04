"""Security review 2026-10-03 (finding 6, MEDIUM): attachment names are paths.

The mail job saved each attachment under the name the SENDER chose, joined to
the landing folder. ``../../x/stmt.pdf`` climbed out of it and an absolute
name replaced it outright; the only filter was the extension. A name is now
reduced to its final segment (no separators, no control characters, no
leading dot) before anything uses it, and the save is confined to its folder.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from core.agents.mail.schema import MailAttachment
from core.engine.runner import run


def _run(tmp_path: Path, *names: str):
    msg = {
        "id": "m1",
        "sender": "billing@acmetooling.com",
        "date": "2026-07-15T12:00:00Z",
        "attachments": [
            {
                "id": f"a-{i}",
                "name": n,
                "content_b64": base64.b64encode(b"pdf-" + n.encode()).decode(),
            }
            for i, n in enumerate(names)
        ],
    }
    messages = tmp_path / "messages.json"
    messages.write_text(json.dumps([msg]))
    return run(
        "demo",
        "mail",
        "fetch",
        params={
            "messages_file": str(messages),
            "landing_dir": str(tmp_path / "work" / "landing"),
            "denied_senders": "clinic.example",
        },
        ledger_dir=tmp_path / "data",
    )


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ("../../statements/stmt.pdf", "stmt.pdf"),
        ("/etc/stmt.pdf", "stmt.pdf"),
        ("..\\..\\stmt.pdf", "stmt.pdf"),
        ("C:\\Users\\x\\stmt.pdf", "stmt.pdf"),
        (".hidden.pdf", "hidden.pdf"),
        ("in\x00voice.pdf", "in-voice.pdf"),
        ("invoice-4471.pdf", "invoice-4471.pdf"),
    ],
)
def test_an_attachment_name_is_one_segment(sent, kept):
    assert MailAttachment(id="a", name=sent).name == kept


@pytest.mark.parametrize("sent", ["..", "../", "/", "", "...."])
def test_a_name_with_nothing_left_falls_back_and_is_filtered(sent):
    assert MailAttachment(id="a", name=sent).name == "attachment.bin"


def test_a_hostile_name_lands_inside_the_landing_folder(tmp_path):
    result = _run(tmp_path, "../../statements/stmt.pdf", "/tmp/abs-stmt.pdf")

    assert result.status == "ok"
    landing = tmp_path / "work" / "landing"
    assert sorted(p.name for p in landing.iterdir()) == ["abs-stmt.pdf", "stmt.pdf"]
    assert not (tmp_path / "statements").exists()
    assert not (tmp_path / "work" / "statements").exists()
