"""Reading on the person's own plan (board t-reader, chunk 1, 2026-10-14).
Caleb has Claude Pro; the box holds no model key. Tim hands his Claude the
next few receipts waiting to be read, as images, and his Claude says what
each reads: day, amount, currency, payee, category. That reading is a
proposal: the day files the receipt in its month (the reviewer sees it on
the card), the amount counts only on the person's yes, and nothing is
guessed by code."""

from __future__ import annotations

import base64
import json

from core.tools.mcp_stdio import McpServer
from core.tools.receipts import ReceiptDesk
from tests.unit.test_catch_up import NOW, _link, _send
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_ministry_lines import _door


def _pile(solo, n: int = 3):  # noqa: F811
    desk = ReceiptDesk()
    door = _door(solo, "daniel-reyes")
    door.receipts.desk = desk
    token = _link(door.call("catch_up_link", {})).split("#", 1)[1]
    for i in range(n):
        _send(solo, desk, token, JPEG + bytes([i]), at=NOW)
    return door


def test_tim_hands_over_the_next_receipts_to_read_as_images(solo):  # noqa: F811
    door = _pile(solo, 3)
    got = door.call("receipts_to_read", {"limit": 2})
    assert len(got["receipts"]) == 2 and got["more"] == 1
    images = got["_content"]
    assert [i["type"] for i in images] == ["image", "image"]
    assert base64.b64decode(images[0]["data"]).startswith(JPEG)
    assert images[0]["mimeType"] == "image/jpeg"


def test_the_images_reach_the_persons_ai_beside_the_words(solo):  # noqa: F811
    door = _pile(solo, 1)
    reply = McpServer(door).handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "receipts_to_read", "arguments": {}},
        }
    )
    content = reply["result"]["content"]
    assert content[0]["type"] == "text" and "_content" not in json.loads(content[0]["text"])
    assert content[1]["type"] == "image"


def test_a_reading_is_a_proposal_until_the_person_says_yes(solo):  # noqa: F811
    door = _pile(solo, 1)
    [one] = door.call("receipts_to_read", {})["receipts"]
    said = door.call(
        "record_reading",
        {
            "line": one["line"],
            "day": "2026-09-30",
            "amount": "18.75",
            "currency": "USD",
            "payee": "Shoprite Maputo",
            "category": "Meals",
        },
    )
    assert said["status"] == "read", said
    assert door.call("receipts_to_read", {})["receipts"] == []
    [month] = door.call("month_report", {"month": "2026-09"})["people"]
    [line] = month["lines"]
    assert line["date"] == "2026-09-30" and line["dollars"] is None
    assert line["read"] == "$18.75 at Shoprite Maputo, Meals"
    assert line["waiting_on"] == "their word on the amount"
    door.call("set_amount", {"line": line["source"], "said": "yes, that's right"})
    [month] = door.call("month_report", {"month": "2026-09"})["people"]
    assert month["lines"][0]["dollars"] == "18.75"


def test_someone_elses_receipts_stay_theirs(solo):  # noqa: F811
    _pile(solo, 1)
    stranger = _door(solo, "linda-park").call("receipts_to_read", {"person": "Nobody Here"})
    assert stranger["status"] == "not_found"
    reviewer = _door(solo, "linda-park").call("receipts_to_read", {"person": "Daniel Reyes"})
    assert len(reviewer["receipts"]) == 1


def test_a_reading_with_a_day_to_come_is_refused(solo):  # noqa: F811
    door = _pile(solo, 1)
    [one] = door.call("receipts_to_read", {})["receipts"]
    said = door.call(
        "record_reading",
        {"line": one["line"], "day": "2027-01-01", "amount": "5", "currency": "USD"},
    )
    assert said["status"] == "not_a_day"


QUIET = "never say it to the person"


def test_a_receipts_line_stays_in_the_calls(solo):  # noqa: F811
    """Found on the real phone 10/14: the person's Claude said "events#17"
    out loud. A line names a receipt for Tim's calls; the person hears its
    day, its amount, and who was paid."""
    from core.tools.catalog import RECEIPTS_TO_READ, RECORD_READING, SPECS

    MONTH_REPORT = next(s for s in SPECS if s.name == "month_report")

    door = _pile(solo, 1)
    got = door.call("receipts_to_read", {})
    assert QUIET in got["say"]
    for spec in (RECEIPTS_TO_READ, RECORD_READING, MONTH_REPORT):
        assert QUIET in spec.description, spec.name
