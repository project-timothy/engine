"""The source watch (chunk 8, the owner's ask of 2026-10-15).

"Source pages for config files need to be able to ingest new and regular
changes automatically in a cron or a forced event. Maybe a hash can look to
see if the document has changed." A rules file names the copy of its
handbook it was read from; every morning, and whenever someone runs it,
``policy watch`` hashes that copy and finds every rule's words in it. A new
copy, or a rule whose words are gone, is one card to whoever approves
rules. No rule goes in or out of force on a copy.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from conftest import minimal_pdf
from core.agents.policy.jobs import CARD_AUTHORITY, COPY_CARD, approved_rule_shas
from core.engine.runner import run
from core.policy import PolicyError, find_quote, parse_layer
from core.tools.decide import done_words, plain_words
from tests.unit.test_door_my_words import no_plan, solo  # noqa: F401  (fixture)
from tests.unit.test_policy_before import RULES
from tests.unit.test_policy_month import _approve_rules, _rules

POLICY = """\
Living Water spending policy

Meals
Meals are reimbursed up to $40 each.
\f
Purchasing
Purchases over $1,000 are approved first.
"""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _copy(solo, raw: bytes, *, name: str = "policy.txt", pin: bytes | None = None) -> None:  # noqa: F811
    folder = Path(solo["root"]) / "living-water" / "rules"
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(raw)
    sha = f', sha = "{_sha(pin)}"' if pin is not None else ""
    _rules(solo, f'copy = {{ file = "{name}"{sha} }}\n' + RULES.lstrip())


def _watch(solo, at: str = ""):  # noqa: F811
    return run(
        "living-water",
        "policy",
        "watch",
        params={"at": at} if at else {},
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )


def _cards(solo) -> list[dict]:  # noqa: F811
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    rows = con.execute(
        "SELECT id, status, params_json FROM approval_queue WHERE action_type = ? ORDER BY id",
        (COPY_CARD,),
    ).fetchall()
    con.close()
    return [{"id": i, "status": s, **json.loads(p)} for i, s, p in rows]


# ---- core/policy: finding a rule's words ------------------------------------------


def test_a_quote_is_found_on_its_page_however_the_text_layer_spaced_it():
    pages = [
        "Meals\nMeals are reim-\nbursed up to\n$40 each.",
        "Purchases over $1,000 are approved first.",
    ]
    assert find_quote("Meals are reimbursed up to $40 each.", pages) == 1
    assert find_quote("Purchases over $1,000 are\napproved first.", pages) == 2
    assert find_quote("“Purchases over $1,000”", ['"Purchases over $1,000"']) == 1


def test_a_quote_may_run_across_a_page_break():
    assert (
        find_quote("up to $40 each. Purchases over", ["Meals up to $40 each.", "Purchases over"])
        == 1
    )


def test_a_changed_word_or_amount_is_not_found():
    pages = ["Meals are reimbursed up to $50 each."]
    assert find_quote("Meals are reimbursed up to $40 each.", pages) is None
    assert find_quote("", pages) is None


def test_naming_the_copy_never_changes_a_rules_fingerprint():
    import tomllib

    plain = parse_layer(tomllib.loads(RULES), "policy.toml")
    named = parse_layer(
        tomllib.loads('copy = { file = "policy.pdf" }\n' + RULES.lstrip()), "policy.toml"
    )
    assert [r.sha for r in plain.rules] == [r.sha for r in named.rules]
    assert named.copy is not None and named.copy.file == "policy.pdf"


@pytest.mark.parametrize(
    ("copy", "says"),
    [
        ('{ file = "a.pdf", url = "https://example.org/a.pdf" }', "a file or a url"),
        ("{ }", "a file or a url"),
        ('{ url = "http://example.org/a.pdf" }', "https"),
        ('{ file = "../authority.toml" }', "under rules/"),
        ('{ file = "/etc/passwd" }', "under rules/"),
        ('{ file = "a.pdf", sha = "abc" }', "64 hex"),
        ('{ file = "a.pdf", page = 3 }', "unknown field"),
    ],
)
def test_a_copy_says_where_it_is_and_nothing_else(copy, says):
    import tomllib

    with pytest.raises(PolicyError, match=says):
        parse_layer(tomllib.loads(f"copy = {copy}\n" + RULES.lstrip()), "policy.toml")


# ---- the watch job -----------------------------------------------------------------


def test_with_no_copy_named_the_watch_reads_nothing(solo):  # noqa: F811
    no_plan(solo)
    _rules(solo, RULES)
    out = _watch(solo)
    assert out.status == "ok"
    assert "no rules file names its copy" in out.summary


def test_the_copy_the_rules_were_drafted_from_raises_nothing(solo):  # noqa: F811
    raw = POLICY.encode()
    _copy(solo, raw, pin=raw)
    out = _watch(solo)
    assert out.status == "ok", out.summary
    assert _cards(solo) == []


def test_a_new_copy_is_one_card_naming_what_still_reads_and_what_does_not(solo):  # noqa: F811
    old = POLICY.encode()
    new = POLICY.replace("$1,000 are approved first", "$1,500 are approved first").encode()
    _copy(solo, new, pin=old)
    _approve_rules(solo)
    in_force = approved_rule_shas(
        sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3"), "living-water"
    )

    out = _watch(solo)

    assert out.status == "needs_approval", out.summary
    [card] = _cards(solo)
    assert card["what"] == "a new copy of Living Water spending policy"
    assert card["gone"] == "supplies-ok-first"
    assert card["still"] == "meals-cap"
    assert card["sha"] == _sha(new)
    assert card["drafted_from"] == _sha(old)
    assert card["lines"].split("; ") == [
        "Not in this copy: Supplies over $1,000 need your reviewer's OK before you buy them. "
        "(Living Water spending policy, Purchasing, p. 4)",
        "Still word for word: A meal is up to $40. (Living Water spending policy, Meals, p. 2)",
    ]
    # Nothing went in or out of force.
    assert (
        approved_rule_shas(
            sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3"), "living-water"
        )
        == in_force
    )
    # The next morning reads the same copy and raises nothing more.
    assert _watch(solo, at="the next morning").status == "ok"
    assert len(_cards(solo)) == 1


def test_another_new_copy_retires_the_card_still_waiting(solo):  # noqa: F811
    old = POLICY.encode()
    _copy(solo, POLICY.replace("$40", "$45").encode(), pin=old)
    _watch(solo)
    _copy(solo, POLICY.replace("$40", "$50").encode(), pin=old)
    _watch(solo)
    cards = _cards(solo)
    assert len(cards) == 2
    assert cards[0]["status"] != "pending"
    assert cards[1]["status"] == "pending"


def test_with_no_sha_named_only_missing_words_raise_a_card(solo):  # noqa: F811
    _copy(solo, POLICY.encode())
    assert _watch(solo).status == "ok"
    assert _cards(solo) == []
    _copy(solo, POLICY.replace("$40", "$45").encode())
    _watch(solo)
    [card] = _cards(solo)
    assert card["what"] == "the copy of Living Water spending policy"
    assert card["gone"] == "meals-cap"


def test_a_pdf_copy_is_read_page_by_page(solo):  # noqa: F811
    writer = PdfWriter()
    for text in (
        "Meals are reimbursed up to $40 each.",
        "Purchases over $1,000 are approved first.",
    ):
        writer.add_page(PdfReader(io.BytesIO(minimal_pdf(text))).pages[0])
    buf = io.BytesIO()
    writer.write(buf)
    pdf = buf.getvalue()
    _copy(solo, pdf, name="policy.pdf", pin=b"the old copy")
    _watch(solo)
    [card] = _cards(solo)
    assert card["gone"] == ""
    assert card["still"] == "meals-cap,supplies-ok-first"


def test_a_copy_that_does_not_read_is_said_and_nothing_is_raised(solo):  # noqa: F811
    _copy(solo, b"", name="policy.txt")
    out = _watch(solo)
    assert out.status == "ok"
    assert [a.code for a in out.anomalies] == ["policy.copy_unreadable"]
    assert _cards(solo) == []


def test_a_copy_at_a_web_address_is_fetched_each_time(solo):  # noqa: F811
    served = {"body": POLICY.encode()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(served["body"])

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/policy.txt"
        _rules(
            solo, f'copy = {{ url = "{url}", sha = "{_sha(POLICY.encode())}" }}\n' + RULES.lstrip()
        )
        assert _watch(solo).status == "ok"
        served["body"] = POLICY.replace("$40", "$45").encode()
        _watch(solo, at="again")  # a forced look
        [card] = _cards(solo)
        assert card["from"] == url
        assert card["gone"] == "meals-cap"
    finally:
        server.shutdown()


# ---- the card ----------------------------------------------------------------------


def test_the_card_goes_to_whoever_approves_rules_and_reads_each_rule_on_its_own_line(solo):  # noqa: F811
    assert CARD_AUTHORITY[COPY_CARD].action == "confirm"
    assert CARD_AUTHORITY[COPY_CARD].money is False
    _copy(solo, POLICY.replace("$40", "$45").encode(), pin=POLICY.encode())
    _watch(solo)
    [card] = _cards(solo)
    row = {"agent": "policy", "action_type": COPY_CARD, "params": card}
    words = plain_words(row, "approve")
    assert words.startswith("Approve a new copy of Living Water spending policy.")
    assert "Not in this copy: A meal is up to $40." in words
    # What the yes leaves in force, on the card and once it is done
    # (the owner's phone run, 2026-10-15: "did the meal rule survive?").
    stays = (
        "No rule changes with this yes; every rule still reads as it was approved, so "
        '"A meal is up to $40" still counts until its new words get their own yes.'
    )
    assert words.endswith(stays)
    assert (
        done_words(row, "approved")
        == f"Approved: a new copy of Living Water spending policy. {stays}"
    )
