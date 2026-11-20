"""Ten missionaries from one list (chunk 11, "ten units in a day").

A sending organization that is already set up brings new people on from one
list, one line each, in the same words setup asks for: "Ivy Chen =
missionary in Thailand, ivy.chen@example.org, looked after by Ann Lee and
Rita Cole". Each line puts the person in authority.toml (their role, their
own unit, the address their welcome link goes to, the zone their place
names), anyone who files reports into the expense tree, and each leader the
line names gains them, so the new missionary's asks reach their own leader
on the first day. The doorkeeper's ``invite-all`` then mails every welcome
link.

The whole list is checked before anything is written: one bad line refuses
the list, naming the line, and every file stays as it was. The same list
twice adds nobody twice.

Set up from the made-up Harvest Fields pack (tests/fixtures/packs/harvest).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from core.engine.cli import main
from core.engine.config import load_tenant
from core.onboarding import OnboardingError, apply, next_question, plan, record
from core.onboarding.roster import add_people
from core.tools.viewer import viewer_for

HARVEST = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "harvest"

PEOPLE = {
    "who": "Our ministry organization",
    "legal_name": "Harvest Fields Mission, Inc.",
    "your_name": "Lydia Brooks",
    "your_role": "finance",
    "timezone": "Central",
    "people": "Ann Lee = team-leader\nRita Cole = regional-director\nSam Ortiz = missionary\n"
    "Abby Stone = member-care",
    "looks_after": "Ann Lee = Sam Ortiz\nRita Cole = Sam Ortiz",
}

TEN = """\
Ivy Chen = missionary in Thailand, ivy.chen@example.org, looked after by Ann Lee and Rita Cole
Omar Haddad = missionary in Jordan, omar.haddad@example.org, looked after by Ann Lee
Grace Mwangi = missionary in Kenya, grace.mwangi@example.org
Tomas Rivera = missionary in Peru, tomas.rivera@example.org
Hana Sato = missionary in Japan, hana.sato@example.org
Luke Brandt = missionary in Germany, luke.brandt@example.org
Esther Mensah = missionary in Ghana, esther.mensah@example.org
Paul Novak = missionary, paul.novak@example.org
Ruth Okoro = missionary in Nigeria, ruth.okoro@example.org
Dan Weller = team-leader, dan.weller@example.org
"""


@pytest.fixture
def harvest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    answers: dict = {"pack": str(HARVEST)}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], PEOPLE.get(q["id"], q.get("default", "")))
    apply(plan(answers, "harvest"), root=root, run_audit=False)
    return root


def _people(root: Path) -> dict:
    return tomllib.loads((root / "harvest" / "authority.toml").read_text())["people"]


def _files(root: Path) -> dict[str, bytes]:
    folder = root / "harvest"
    return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


def test_ten_people_come_on_from_one_list(harvest):
    added = add_people("harvest", TEN, tenants_root=harvest)
    assert len(added.added) == 10 and added.already == ()
    people = _people(harvest)
    ivy = people["ivy-chen"]
    assert ivy["roles"] == ["missionary"]
    assert ivy["scopes"] == ["ivy-chen"]
    assert ivy["email"] == "ivy.chen@example.org"
    assert ivy["zone"] == "Asia/Bangkok"
    # No place named: the ministry's own zone.
    assert people["paul-novak"]["zone"] == "America/Chicago"
    assert people["dan-weller"]["roles"] == ["team-leader"]
    # Everyone who files reports is in the expense tree; a leader is not.
    filers = {p.name for p in load_tenant("harvest", tenants_root=harvest).expenses.persons}
    assert {"Ivy Chen", "Ruth Okoro", "Paul Novak"} <= filers
    assert "Dan Weller" not in filers
    # Each one is someone Tim can answer as.
    for pid in ("ivy-chen", "omar-haddad", "dan-weller"):
        assert viewer_for("harvest", pid, tenants_root=harvest) is not None


def test_each_leader_named_gains_the_new_missionary(harvest):
    add_people("harvest", TEN, tenants_root=harvest)
    people = _people(harvest)
    assert people["ann-lee"]["scopes"] == ["ann-lee", "sam-ortiz", "ivy-chen", "omar-haddad"]
    assert people["rita-cole"]["scopes"] == ["rita-cole", "sam-ortiz", "ivy-chen"]


def test_a_leader_can_come_on_in_the_same_list(harvest):
    add_people(
        "harvest",
        "Mia Ross = missionary, mia.ross@example.org, looked after by Dan Weller\n"
        "Dan Weller = team-leader, dan.weller@example.org\n",
        tenants_root=harvest,
    )
    assert _people(harvest)["dan-weller"]["scopes"] == ["dan-weller", "mia-ross"]


def test_the_same_list_twice_adds_nobody_twice(harvest):
    add_people("harvest", TEN, tenants_root=harvest)
    before = _files(harvest)
    again = add_people("harvest", TEN, tenants_root=harvest)
    assert again.added == ()
    assert len(again.already) == 10
    assert _files(harvest) == before


@pytest.mark.parametrize(
    ("line", "says"),
    [
        ("Zed Park, zed@example.org", "name = what they do"),
        ("Zed Park = astronaut", "astronaut"),
        ("Zed Park = missionary, looked after by Nobody Here", "Nobody Here"),
        ("Zed Park = missionary, looked after by Sam Ortiz", "files their own reports"),
        ("Zed Park = missionary, ivy.chen@example.org", "ivy.chen@example.org"),
        ("Zed Park = missionary, abby.stone@example.org", "abby.stone@example.org"),
    ],
)
def test_one_bad_line_refuses_the_list_and_writes_nothing(harvest, line, says):
    authority = harvest / "harvest" / "authority.toml"
    text = authority.read_text()
    authority.write_text(
        text.replace(
            'roles = ["member-care"]', 'roles = ["member-care"]\nemail = "abby.stone@example.org"'
        )
    )
    before = _files(harvest)
    with pytest.raises(OnboardingError, match=says):
        add_people("harvest", TEN + line, tenants_root=harvest)
    assert _files(harvest) == before


def test_a_check_says_who_would_come_on_and_writes_nothing(harvest):
    before = _files(harvest)
    checked = add_people("harvest", TEN, tenants_root=harvest, check=True)
    assert [n.pid for n in checked.added][:2] == ["ivy-chen", "omar-haddad"]
    assert checked.added[0].looked_after_by == ("Ann Lee", "Rita Cole")
    assert _files(harvest) == before


def test_the_command_reads_the_list_and_says_what_comes_next(harvest, tmp_path, capsys):
    roster = tmp_path / "roster.txt"
    roster.write_text(TEN)
    assert main(["people", "add", "harvest", str(roster)]) == 0
    out = capsys.readouterr().out
    assert "Ivy Chen" in out and "looked after by Ann Lee and Rita Cole" in out
    assert "invite-all" in out
    assert main(["people", "add", "harvest", str(roster)]) == 0
    assert "already here" in capsys.readouterr().out
