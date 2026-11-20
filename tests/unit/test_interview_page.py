"""The missionary's own setup questions as pages on her phone (board c49).

The link's token rides in the fragment, which the start page's one script
posts, so no URL a server sees carries it. Every other page is plain HTML:
no script, nothing loaded from outside. The letter's photo comes as a
multipart form. Only this box's own pages may post.
"""

from __future__ import annotations

import html
import re
import threading
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlencode

import pytest

from core.engine import cli
from core.onboarding import interview as iv
from core.tools import interview_page as ip
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)

ORIGIN = "https://tim.example.org"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def book(tmp_path):
    return iv.InterviewBook(tmp_path / "interviews")


def _token(book) -> str:
    return book.mint(name="Hannah Cole", email="hannah@example.org", by="Paul Weston", now=1_000)


def _post(book, path, form, *, headers=FORM, now=2_000.0):
    raw = urlencode(form, doseq=True).encode()
    return ip.answer(book, path, headers, raw, ORIGIN, now=lambda: now)


def _text(data: bytes) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", data.decode())))


def test_the_start_page_reads_the_link_from_the_fragment_only():
    html = ip.start_page("n0nce")
    assert "location.hash" in html and ip.NEXT_PATH in html
    assert "history.replaceState" in html  # the token leaves the address bar
    assert "script-src 'nonce-n0nce'" in ip.csp("n0nce", script=True)
    assert "script-src 'none'" in ip.csp("n0nce")


def test_the_link_opens_her_first_screen(book):
    token = _token(book)
    status, headers, data = _post(book, ip.NEXT_PATH, {"token": token})
    assert status == 200
    assert "How does support reach you?" in _text(data)
    assert "Into my ministry's account" in _text(data)
    assert "script-src 'none'" in headers["Content-Security-Policy"]
    assert "<script" not in data.decode()
    assert headers["Cache-Control"] == "no-store"


def test_a_made_up_or_run_out_link_says_so(book):
    status, _, data = _post(book, ip.NEXT_PATH, {"token": "x" * 32})
    assert status == 400 and "run out" in _text(data)
    token = _token(book)
    status, _, _ = _post(book, ip.NEXT_PATH, {"token": token}, now=1_000 + 15 * 86_400)
    assert status == 400


def test_another_site_cannot_post(book):
    token = _token(book)
    status, _, _ = _post(
        book, ip.NEXT_PATH, {"token": token}, headers={**FORM, "Origin": "https://evil.example"}
    )
    assert status == 403


def test_an_answer_draws_the_next_screen_and_a_wrong_one_asks_again(book):
    token = _token(book)
    _, _, data = _post(book, ip.ANSWER_PATH, {"token": token, "screen": "support"})
    assert "Pick at least one." in _text(data)
    form = {"token": token, "screen": "support", "support": ["own_nonprofit", "platform"]}
    _, _, data = _post(book, ip.ANSWER_PATH, form)
    assert "Take a picture of your IRS letter." in _text(data)
    assert book.answers(token)["support"] == ["own_nonprofit", "platform"]


def _multipart(fields: dict, photo: bytes) -> tuple[dict, bytes]:
    edge = uuid.uuid4().hex
    parts = [
        f'--{edge}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items()
    ]
    parts.append(
        f'--{edge}\r\nContent-Disposition: form-data; name="photo"; filename="l.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n".encode()
        + photo
        + b"\r\n"
    )
    body = b"".join(parts) + f"--{edge}--\r\n".encode()
    return {"Content-Type": f"multipart/form-data; boundary={edge}"}, body


def test_the_letter_comes_with_its_photo(book):
    token = _token(book)
    _post(book, ip.ANSWER_PATH, {"token": token, "screen": "support", "support": "own_nonprofit"})
    photo = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4
    headers, body = _multipart(
        {"token": token, "screen": "letter", "legal_name": "Hope Springs Ministries Inc."}, photo
    )
    status, _, data = ip.answer(book, ip.ANSWER_PATH, headers, body, ORIGIN, now=lambda: 2_000.0)
    assert status == 200 and "Where do you live and work?" in _text(data)
    assert book.letter(token) == photo


def test_she_walks_every_screen_to_done(book):
    token = _token(book)
    steps = [
        {"screen": "support", "support": "own_nonprofit"},
        {"screen": "letter", "legal_name": "Hope Springs Ministries Inc."},
        {"screen": "where", "town": "Nakuru, Kenya"},
        {
            "screen": "reviewer",
            "reviewer_name": "Sarah Whitfield",
            "reviewer_email": "sarah@example.org",
            "approves": "approve",
        },
        {"screen": "driving", "drives": "yes"},
    ]
    for step in steps:
        _, _, data = _post(book, ip.ANSWER_PATH, {"token": token, **step})
    said = _text(data)
    assert "Here's what I heard." in said
    assert "Hannah Cole, in Nakuru, Kenya" in said
    assert "Sarah Whitfield, who also approves each month" in said
    assert "Up to $250, your word is enough." in said
    _, _, data = _post(book, ip.ANSWER_PATH, {"token": token, "screen": "heard", "right": "yes"})
    assert "Thank you, Hannah. Paul sets up your books next" in _text(data)
    assert book.status(token) == "done"
    [row] = book.rows()
    assert row["file"].endswith(".ministry.toml")


def test_no_nonprofit_of_her_own_is_said_plainly(book):
    token = _token(book)
    _, _, data = _post(
        book, ip.ANSWER_PATH, {"token": token, "screen": "support", "support": "straight"}
    )
    said = _text(data)
    assert "I can't set up your books by myself yet" in said and "Paul will be in touch" in said


def test_a_church_is_left_for_the_maintainer(book):
    token = _token(book)
    _, _, data = _post(book, ip.ANSWER_PATH, {"token": token, "screen": "support", "church": "yes"})
    assert "A church or an organization is set up with Paul" in _text(data)
    assert book.status(token) == "not_yet"


def test_the_command_makes_a_link_and_lists_it(tmp_path, capsys):
    ledger = tmp_path / "ledger"
    args = [
        "interview",
        "new",
        "--name",
        "Hannah Cole",
        "--email",
        "hannah@example.org",
        "--by",
        "Paul Weston",
        "--base",
        "https://practice.asktimothy.org/",
        "--ledger-dir",
        str(ledger),
    ]
    assert cli.main(args) == 0
    link = capsys.readouterr().out.strip()
    assert re.fullmatch(r"https://practice\.asktimothy\.org/start#[A-Za-z0-9_-]{32}", link)
    assert cli.main(["interview", "list", "--ledger-dir", str(ledger)]) == 0
    listed = capsys.readouterr().out
    assert "open" in listed and "Hannah Cole <hannah@example.org>" in listed
    assert link.split("#")[1] not in listed
    assert (ledger / iv.INTERVIEWS).is_dir()


def test_the_command_refuses_a_plain_http_address(tmp_path):
    args = ["interview", "new", "--name", "H", "--email", "h@example.org", "--by", "D"]
    args += ["--base", "http://example.org", "--ledger-dir", str(tmp_path)]
    assert cli.main(args) == 2


# ---- over HTTP, on the box's door


@pytest.fixture
def door(solo, tmp_path):  # noqa: F811
    from core.tools.mcp_http import TokenFile, make_server

    server = make_server(
        "living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        obligations_file=None,
        verifier=TokenFile(tmp_path / "tokens.json"),
        resource=ORIGIN + "/mcp",
        authorization_servers=[ORIGIN],
        port=0,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    book = iv.InterviewBook(solo["ledger_root"].parent / iv.INTERVIEWS)
    try:
        yield {"base": f"http://127.0.0.1:{server.server_address[1]}", "book": book}
    finally:
        server.shutdown()


def _http(door, path, form=None):
    data = urlencode(form, doseq=True).encode() if form is not None else None
    req = urllib.request.Request(door["base"] + path, data=data, headers=FORM if data else {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


def test_the_box_serves_the_start_page_and_her_screens(door):
    import time

    status, headers, html = _http(door, ip.START_PATH)
    assert status == 200 and "location.hash" in html
    assert "script-src 'nonce-" in headers["Content-Security-Policy"]
    token = door["book"].mint(
        name="Hannah Cole", email="hannah@example.org", by="Paul Weston", now=time.time()
    )
    status, _, html = _http(door, ip.NEXT_PATH, {"token": token})
    assert status == 200 and "How does support reach you?" in html


def test_whoever_sent_the_link_hears_once_she_finished_or_stopped(book):
    done = _token(book)
    for step in (
        {"screen": "support", "support": "own_nonprofit"},
        {"screen": "letter", "legal_name": "Hope Springs Ministries Inc."},
        {"screen": "where", "town": "Kenya"},
        {"screen": "reviewer", "nobody": "yes"},
        {"screen": "driving", "drives": "no"},
        {"screen": "heard", "right": "yes"},
    ):
        _post(book, ip.ANSWER_PATH, {"token": done, **step})
    stopped = book.mint(name="Joel Park", email="joel@example.org", by="Paul Weston", now=1_000)
    _post(book, ip.ANSWER_PATH, {"token": stopped, "screen": "support", "support": "straight"})
    book.mint(name="Ann Lee", email="ann@example.org", by="Paul Weston", now=1_000)  # still open
    said = sorted(iv.notices(book, since=1_500), key=lambda n: n["about"])
    assert [(n["about"], n["stands"], n["to"], n["kind"]) for n in said] == [
        ("Hannah Cole", "done", "paul-weston", "interview"),
        ("Joel Park", "not_yet", "paul-weston", "interview"),
    ]
    assert iv.notices(book, since=3_000) == []
    assert all(len(n["id"]) < 60 for n in said)


def test_engine_notices_carries_them(tmp_path, capsys):
    import json
    import time

    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    ledger = tmp_path / "ledger"
    book = iv.InterviewBook(resolve_ledger_root(iv.INTERVIEWS, ledger))
    token = book.mint(name="Joel Park", email="joel@example.org", by="Paul Weston", now=time.time())
    book.take(token, "support", {"support": ["straight"]}, now=time.time())
    with Ledger.open(resolve_ledger_root("t", ledger)):
        pass  # a box's books, with nothing in them yet
    assert cli.main(["notices", "t", "--ledger-dir", str(ledger)]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [(n["kind"], n["about"], n["to"]) for n in lines] == [
        ("interview", "Joel Park", "paul-weston")
    ]
