"""The routing lane and the route commands (#436), end to end on a tenant
with authority.toml: reminders on the clock, never about the person; the
backup only when turned on; upward only in the organization shape;
handoff, "on it by", and dated delegation as a person's acts."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from core.agents.brief import jobs as brief_jobs
from core.engine import cli
from core.engine.cli import main
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

REPO = Path(__file__).resolve().parents[4]
TODAY = datetime.now(UTC).date()

POLICY = """
[money]
out = "human"

[safeguards]
self_approval = "never"
second_approver_above = 0

[roles.owner]
grants = ["*:*"]

[roles.approver]
grants = ["view:*", "approve:*"]

[roles.board]
grants = ["view:*", "approve:*"]

[people.dana]
roles = ["owner"]

[people.sam]
roles = ["approver"]
backup = "lee"

[people.lee]
roles = ["approver"]

[people.kim]
roles = ["board"]

[routing]
owner = "dana"
ROUTING_EXTRA

[[routing.route]]
resource = "payment"
steps = [{ role = "approver" }]
"""


def _write(world, extra: str = "", shape: str | None = None) -> None:
    demo = world["root"] / "demo"
    (demo / "authority.toml").write_text(POLICY.replace("ROUTING_EXTRA", extra))
    if shape:
        toml = demo / "tenant.toml"
        text = toml.read_text()
        toml.write_text(text.replace('shape = "commercial-small"', f'shape = "{shape}"'))


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "tenants"
    shutil.copytree(REPO / "tenants" / "demo", root / "demo")
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setattr(cli, "_operator_at_terminal", lambda: True)
    w = {"tmp": tmp_path, "root": root, "data": tmp_path / "data"}
    _write(w)
    # A first run, so cards have a run to hang on.
    run("demo", "routing", "tick", params={"today": TODAY.isoformat()}, ledger_dir=w["data"])
    return w


def _ledger(world):
    return Ledger.open(resolve_ledger_root("demo", world["data"]))


def _card(world, params: dict | None = None) -> int:
    with _ledger(world) as ledger:
        run_id = ledger.conn.execute("SELECT MIN(id) FROM runs").fetchone()[0]
        ledger.enqueue_approval(
            idempotency_key=f"t:{json.dumps(params or {}, sort_keys=True)}",
            run_id=run_id,
            tenant="demo",
            agent="ap",
            action_type="ap.payment_recommendation",
            params={"payee": "Acme", "invoice_ref": "A-1", "amount": "100.00", **(params or {})},
        )
        return ledger.list_approvals("demo")[-1]["id"]


def _tick(world, days: int):
    today = (TODAY + timedelta(days)).isoformat()
    return run("demo", "routing", "tick", params={"today": today}, ledger_dir=world["data"])


def _reminders(world) -> list[dict]:
    with _ledger(world) as ledger:
        rows = ledger.conn.execute(
            "SELECT payload_json FROM events WHERE event_type = 'route.reminder' ORDER BY id"
        ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def _q(world, *argv: str) -> int:
    verb, *rest = argv
    return main(["queue", verb, "demo", *rest, "--ledger-dir", str(world["data"])])


def test_no_authority_file_routes_nothing(world):
    (world["root"] / "demo" / "authority.toml").unlink()
    _card(world)
    out = _tick(world, 30)
    assert out.status == "ok" and "nothing routes" in out.summary
    assert _reminders(world) == []


def test_the_first_reminder_goes_to_the_approvers_alone_once(world):
    _card(world)
    assert _tick(world, 1).summary == "routing: 0 reminder(s) recorded"
    _tick(world, 2)
    first = _reminders(world)
    assert {r["to"] for r in first} == {"sam", "lee"}
    assert {r["kind"] for r in first} == {"reminder"}
    assert all("your decision" in r["text"] for r in first)
    _tick(world, 3)
    assert len(_reminders(world)) == 2  # once per person and level


def test_the_second_reminder_offers_a_handoff(world):
    _card(world)
    _tick(world, 4)
    texts = [r["text"] for r in _reminders(world) if r["kind"] == "handoff_offer"]
    assert texts and all("someone else" in t and "on-it" in t for t in texts)


def test_no_reminder_names_who_has_not_acted_to_anyone_else(world):
    _write(world, "backup = true")
    _card(world)
    _tick(world, 5)
    for r in _reminders(world):
        others = {"sam", "lee", "dana", "kim"} - {r["to"]}
        if r["kind"] == "covering_for":
            assert "Covering for" in r["text"]
            continue
        assert not any(name in r["text"].split("--as")[0] for name in others)


def test_the_backup_is_covering_for_and_only_when_turned_on(world):
    _card(world)
    _tick(world, 5)
    assert not [r for r in _reminders(world) if r["kind"] == "covering_for"]
    _write(world, "backup = true")
    _tick(world, 6)
    covering = [r for r in _reminders(world) if r["kind"] == "covering_for"]
    assert [r["to"] for r in covering] == ["lee"]
    assert covering[0]["text"].startswith("Covering for sam")
    assert "overdue" not in covering[0]["text"].lower()


def test_upward_only_in_the_organization_shape(world):
    _write(world, 'upward = true\nupward_role = "board"')
    _card(world)
    _tick(world, 5)
    assert not [r for r in _reminders(world) if r["kind"] == "upward"]
    _write(world, 'upward = true\nupward_role = "board"', shape="commercial-organization")
    _tick(world, 6)
    up = [r for r in _reminders(world) if r["kind"] == "upward"]
    assert [r["to"] for r in up] == ["kim"]
    assert "to stay on time" in up[0]["text"]


def test_a_handoff_sends_the_reminders_to_the_one_who_took_it(world, capsys):
    card = _card(world)
    assert _q(world, "handoff", "--id", str(card), "--as", "sam", "--to", "lee") == 0
    _tick(world, 2)
    assert {r["to"] for r in _reminders(world)} == {"lee"}


def test_a_handoff_goes_only_to_someone_who_can_decide(world, capsys):
    card = _card(world)
    assert _q(world, "handoff", "--id", str(card), "--as", "sam", "--to", "kim") == 2
    assert "cannot decide" in capsys.readouterr().err


def test_on_it_by_a_date_pauses_the_clock(world):
    card = _card(world)
    by = (TODAY + timedelta(6)).isoformat()
    assert _q(world, "on-it", "--id", str(card), "--as", "sam", "--by", by) == 0
    _tick(world, 4)
    assert _reminders(world) == []


def test_a_person_act_needs_a_terminal(world, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_operator_at_terminal", lambda: False)
    card = _card(world)
    assert _q(world, "on-it", "--id", str(card), "--as", "sam", "--by", "2099-01-01") == 2
    assert "terminal" in capsys.readouterr().err


def test_a_delegate_decides_on_the_delegators_behalf_and_the_card_says_so(world):
    until = (TODAY + timedelta(10)).isoformat()
    assert _q(world, "delegate", "--as", "sam", "--to", "kim", "--role", "approver",
              "--until", until) == 0  # fmt: skip
    card = _card(world)
    assert _q(world, "approve", "--id", str(card), "--as", "kim") == 0
    with _ledger(world) as ledger:
        (row,) = [r for r in ledger.list_approvals("demo") if r["id"] == card]
    assert row["status"] == "approved"
    assert row["params"]["on_behalf_of"] == "sam"
    assert row["params"]["decided_by"] == "kim"


def test_a_delegation_of_a_role_you_do_not_hold_is_refused(world, capsys):
    until = (TODAY + timedelta(10)).isoformat()
    assert _q(world, "delegate", "--as", "kim", "--to", "lee", "--role", "approver",
              "--until", until) == 2  # fmt: skip
    assert "directly" in capsys.readouterr().out


def test_a_revoked_delegation_stops_at_once(world, capsys):
    until = (TODAY + timedelta(10)).isoformat()
    _q(world, "delegate", "--as", "sam", "--to", "kim", "--role", "approver", "--until", until)
    did = f"sam:kim:approver:{TODAY.isoformat()}"
    assert _q(world, "revoke", "--as", "lee", "--delegation", did) == 2
    assert _q(world, "revoke", "--as", "sam", "--delegation", did) == 0
    card = _card(world, {"invoice_ref": "A-2"})
    capsys.readouterr()
    assert _q(world, "approve", "--id", str(card), "--as", "kim") == 2
    assert "does not hold" in capsys.readouterr().err


def test_waiting_lists_what_waits_on_a_person_with_their_reminder(world, capsys):
    card = _card(world)
    _tick(world, 2)
    capsys.readouterr()
    assert _q(world, "waiting", "--as", "sam") == 0
    out = capsys.readouterr().out
    assert f"#{card}" in out and "your decision" in out
    assert _q(world, "waiting", "--as", "kim") == 0
    assert "nothing is waiting on kim" in capsys.readouterr().out


def test_the_route_commands_refuse_without_an_authority_file(world, capsys):
    (world["root"] / "demo" / "authority.toml").unlink()
    assert _q(world, "waiting", "--as", "sam") == 2
    assert "no authority.toml" in capsys.readouterr().err


# ---- the brief's line, for the process owner only ------------------------------------


class _Mailer:
    def send_mail(self, **kw):
        pass


def _brief(world) -> str:
    out = world["tmp"] / "briefs"
    ob = world["tmp"] / "ob.toml"
    ob.write_text("")
    run(
        "demo",
        "brief",
        "weekly",
        params={"today": TODAY.isoformat(), "obligations_file": str(ob), "dir": str(out)},
        ledger_dir=world["data"],
    )
    return next(out.glob("brief-*.md")).read_text()


def test_the_owners_brief_says_what_waits_on_whom(world, monkeypatch):
    monkeypatch.setattr(brief_jobs, "_send_client", lambda ctx: _Mailer())
    card = _card(world)
    page = _brief(world)
    assert "## Waiting on others" in page
    assert f"Card #{card}: ap.payment_recommendation, step 1 of 1, waiting on sam, lee" in page


def test_no_owner_no_line(world, monkeypatch):
    monkeypatch.setattr(brief_jobs, "_send_client", lambda ctx: _Mailer())
    policy = (world["root"] / "demo" / "authority.toml").read_text()
    (world["root"] / "demo" / "authority.toml").write_text(policy.replace('owner = "dana"', ""))
    _card(world)
    assert "Waiting on others" not in _brief(world)


def test_a_far_deadline_is_the_clock(world):
    far = (TODAY + timedelta(40)).isoformat()
    _card(world, {"due_date": far})
    _tick(world, 10)
    assert _reminders(world) == []
    assert date.fromisoformat(far) > TODAY
