"""The receipt page on the home screen (chunk 10; the owner's yes of
2026-10-22 to a standing link).

A home-screen icon needs an address that doesn't expire, so each person
who files receipts may hold one standing link, kept only as its hash in
``receipt_links.toml`` and retired in one line. It can do one thing: each
time the page opens, it trades its key for an ordinary one-use photo ticket
(fifteen minutes, the person's own folder), at most so many an hour. A
receipt commits no money, so it takes no Face ID, the same as Tim's own
receipt links. The first open from the home screen tells setup the step
works."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.engine.config import tenant_dir
from core.onboarding.person import SetupBook
from core.tools.receipts import (
    LINKS_FILE,
    HomeLinks,
    ReceiptDesk,
    add_home_link,
    drop_home_link,
    home_person,
)
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _folder(solo) -> Path:  # noqa: F811
    return tenant_dir("living-water", tenants_root=solo["root"])


def test_a_standing_link_is_kept_only_as_its_hash(solo):  # noqa: F811
    key = add_home_link(_folder(solo), "daniel-reyes")
    assert key not in (_folder(solo) / LINKS_FILE).read_text()
    assert home_person(_folder(solo), key) == "daniel-reyes"
    newer = add_home_link(_folder(solo), "daniel-reyes")
    assert home_person(_folder(solo), key) is None  # one per person
    assert drop_home_link(_folder(solo), "daniel-reyes") == 1
    assert home_person(_folder(solo), newer) is None


@pytest.fixture
def box(solo, tmp_path):  # noqa: F811
    from core.tools.mcp_http import TokenFile, make_server

    desk = ReceiptDesk()
    server = make_server(
        "living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        obligations_file=None,
        verifier=TokenFile(tmp_path / "tokens.json"),
        resource="https://tim.example.org/mcp",
        authorization_servers=["https://signin.example.org"],
        port=0,
        receipts=desk,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {**solo, "base": f"http://127.0.0.1:{server.server_address[1]}", "desk": desk}
    finally:
        server.shutdown()


def _get(box, path: str) -> tuple[int, dict, bytes]:
    try:
        with urllib.request.urlopen(box["base"] + path) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _ticket(box, key: str, standalone: bool = False) -> tuple[int, dict]:
    req = urllib.request.Request(
        box["base"] + f"/receipt/k/{key}",
        data=f"standalone={int(standalone)}".encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _send(box, ticket: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        box["base"] + "/receipt",
        data=JPEG,
        method="POST",
        headers={"Content-Type": "image/jpeg", "X-Receipt-Ticket": ticket},
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read())


def test_the_home_page_opens_the_camera_and_can_be_added_to_the_home_screen(box):
    key = add_home_link(_folder(box), "daniel-reyes")
    status, headers, body = _get(box, f"/receipt/k/{key}")
    page = body.decode()
    assert status == 200
    assert 'capture="environment"' in page
    assert f'rel="manifest" href="/receipt/k/{key}/app.webmanifest"' in page
    assert 'rel="apple-touch-icon" href="/receipt/icon-180.png"' in page
    assert "Add to Home Screen" in page
    assert "manifest-src 'self'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer"


def test_the_home_screen_opens_straight_to_the_persons_own_page(box):
    key = add_home_link(_folder(box), "daniel-reyes")
    status, _headers, body = _get(box, f"/receipt/k/{key}/app.webmanifest")
    made = json.loads(body)
    assert status == 200
    assert made["start_url"] == made["scope"] == f"/receipt/k/{key}"
    assert made["display"] == "standalone"
    assert {i["src"] for i in made["icons"]} == {"/receipt/icon-192.png", "/receipt/icon-512.png"}


@pytest.mark.parametrize("size", ["180", "192", "512"])
def test_the_icons_are_tims_mark(box, size):
    status, headers, body = _get(box, f"/receipt/icon-{size}.png")
    assert status == 200 and headers["Content-Type"] == "image/png"
    assert body.startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "path", ["/receipt/icon-64.png", "/receipt/k/short", "/receipt/k/../../authority.toml"]
)
def test_nothing_else_is_served_there(box, path):
    assert _get(box, path)[0] == 404


def test_each_open_trades_the_key_for_a_one_use_photo_ticket(box):
    key = add_home_link(_folder(box), "daniel-reyes")
    status, said = _ticket(box, key)
    assert status == 200
    sent, reply = _send(box, said["ticket"])
    assert sent == 200, reply
    filed = [p for p in Path.cwd().rglob("*.jpg") if "/receipts/Daniel Reyes/" in str(p)]
    assert len(filed) == 1  # his own receipts, filed by intake like any with no amount said
    with pytest.raises(urllib.error.HTTPError):
        _send(box, said["ticket"])  # the ticket is spent; the key is not
    assert _ticket(box, key)[0] == 200


def test_a_retired_key_gets_nothing(box):
    key = add_home_link(_folder(box), "daniel-reyes")
    drop_home_link(_folder(box), "daniel-reyes")
    status, said = _ticket(box, key)
    assert status == 404
    assert "ask" in said["say"].casefold()
    assert _ticket(box, "A" * 32)[0] == 404


def test_tickets_are_rationed_per_key():
    desk = ReceiptDesk()
    links = HomeLinks(
        "living-water",
        tenants_root=None,
        desk=desk,
        per_hour=3,
        person_for=lambda key: "daniel-reyes" if key == "k" else None,
        name_for=lambda person: "Daniel Reyes",
        clock=lambda: 1000.0,
    )
    assert [links.ticket("k", standalone=False)[0] for _ in range(3)] == [200, 200, 200]
    status, said = links.ticket("k", standalone=False)
    assert status == 429 and said["say"]


def test_the_first_open_from_the_home_screen_finishes_that_setup_step(box):
    book = SetupBook(_folder(box))
    book.record("daniel-reyes", "home_screen", "yes", now="2026-10-22T14:00:00+00:00")
    key = add_home_link(_folder(box), "daniel-reyes")
    _ticket(box, key, standalone=False)
    assert "seen" not in book.answers("daniel-reyes")["home_screen"]  # opened in the browser
    _ticket(box, key, standalone=True)
    assert book.answers("daniel-reyes")["home_screen"]["seen"]


def test_the_ministry_retires_a_link_in_one_line(solo, capsys):  # noqa: F811
    from core.engine.cli import main

    assert main(["receipt-link", "living-water", "daniel-reyes", "--domain", "tim.org"]) == 0
    key = capsys.readouterr().out.strip().rsplit("/", 1)[1]
    assert home_person(_folder(solo), key) == "daniel-reyes"
    assert main(["receipt-link", "living-water", "daniel-reyes", "--revoke"]) == 0
    assert "1 receipt link retired" in capsys.readouterr().out
    assert home_person(_folder(solo), key) is None
