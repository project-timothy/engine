"""brief/weekly: Tim's first voice, one plain page a week (Ask Tim build order #2).

The promises under test:

1. The brief says what needs the person first: a card waiting for a yes, a
   deadline overdue or in its final week. Then what is coming, the money, and
   whether the engine itself is healthy. When nothing needs them, it says so.
2. Every fact comes from the read-only tools (core/tools): code computes, the
   page only arranges. Money is exact.
3. One brief per ISO week: later runs that week replay; a new week writes anew.
4. Sending is an external act: with recipients configured, an approval card is
   parked, an approved card sends exactly once, and a tenant's own policy may
   name the send as unattended. Shadow writes and sends nothing.
"""

from __future__ import annotations

import pytest

from core.agents.ap import store
from core.agents.brief import jobs as brief_jobs
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

OBLIGATIONS = """
[[obligation]]
id = "permit"
title = "Work permit renewal"
who = "Jane"
due = 2026-10-09

[[obligation]]
id = "report"
title = "Quarterly support report"
due = 2026-11-02
lead_days = [30, 7]

[[obligation]]
id = "far"
title = "Passport renewal"
due = 2027-06-01
"""


class FakeMailer:
    def __init__(self):
        self.sent: list[dict] = []

    def send_mail(self, *, subject, body, to, attachments=()):
        self.sent.append({"subject": subject, "body": body, "to": list(to)})


@pytest.fixture
def world(tmp_path, monkeypatch):
    ledger_dir = tmp_path / "data"
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor="Acme Tooling",
            invoice_number="A-100",
            amount_cents=123456,
            due_date="2026-10-30",
        )
    ob = tmp_path / "obligations.toml"
    ob.write_text(OBLIGATIONS)
    mailer = FakeMailer()
    monkeypatch.setattr(brief_jobs, "_send_client", lambda ctx: mailer)
    return {"tmp": tmp_path, "ledger_dir": ledger_dir, "root": root, "ob": ob, "mailer": mailer}


def _run(world, today="2026-10-05", *, shadow=False, **params):
    base = {
        "today": today,
        "obligations_file": str(world["ob"]),
        "dir": str(world["tmp"] / "briefs"),
    }
    return run(
        "demo",
        "brief",
        "weekly",
        params={**base, **params},
        ledger_dir=world["ledger_dir"],
        shadow=shadow,
    )


def _page(world, week="2026-W41") -> str:
    return (world["tmp"] / "briefs" / f"brief-{week}.md").read_text()


def _cards(world, status=None):
    with Ledger.open(world["root"]) as ledger:
        sql = "SELECT id, status, params_json FROM approval_queue WHERE action_type = ?"
        rows = ledger.conn.execute(sql, (brief_jobs.SEND_ACTION,)).fetchall()
    return [dict(r) for r in rows if status is None or r["status"] == status]


def _decide(world, card_id, status):
    with Ledger.open(world["root"]) as ledger:
        ledger.conn.execute("UPDATE approval_queue SET status = ? WHERE id = ?", (status, card_id))
        ledger.conn.commit()


# ---- 1 and 2: what the page says ------------------------------------------------


def test_the_page_leads_with_what_needs_the_person(world):
    result = _run(world)
    assert result.status == "ok"
    page = _page(world)
    needs = page.index("## Needs you")
    coming = page.index("## Coming up")
    assert needs < coming < page.index("## Money") < page.index("## The engine")
    # the permit is 4 days out: final week, so it needs the person
    assert "Work permit renewal (Jane): due 9 Oct 2026, in 4 days" in page[needs:coming]
    # the report is 28 days out, inside its 30-day window: coming up, not urgent
    assert "Quarterly support report: due 2 Nov 2026, in 28 days" in page[coming:]
    # the passport is outside every window: not on the page yet
    assert "Passport renewal" not in page


def test_money_is_exact_and_sourced_from_the_books(world):
    _run(world)
    page = _page(world)
    assert "1 open payable, $1,234.56 in all" in page
    assert "Acme Tooling A-100, $1,234.56, due 30 Oct 2026" in page


def test_a_quiet_week_says_so(world, tmp_path):
    empty = tmp_path / "none.toml"
    empty.write_text("")
    _run(world, obligations_file=str(empty))
    page = _page(world)
    assert "Nothing is waiting on you this week." in page


def test_a_job_that_failed_is_named(world):
    with Ledger.open(world["root"]) as ledger:
        ledger.conn.execute(
            "INSERT INTO runs (idempotency_key, tenant, agent, job, status, shadow, result_json, "
            "summary, created_at) VALUES ('r1','demo','ap','intake','error',0,'{}',"
            "'job failed: boom','2026-10-05T12:00:00+00:00')"
        )
        ledger.conn.commit()
    _run(world)
    assert "ap/intake ended in error" in _page(world)


# ---- 3: one per week ---------------------------------------------------------------


def test_one_brief_per_week(world):
    _run(world, "2026-10-05")
    _run(world, "2026-10-07")  # same ISO week: replay
    assert len(list((world["tmp"] / "briefs").glob("*.md"))) == 1
    _run(world, "2026-10-12")
    assert sorted(p.name for p in (world["tmp"] / "briefs").glob("*.md")) == [
        "brief-2026-W41.md",
        "brief-2026-W42.md",
    ]


# ---- 4: sending ----------------------------------------------------------------------


def test_no_recipients_means_no_card_and_no_send(world):
    _run(world)
    assert _cards(world) == [] and world["mailer"].sent == []


def test_recipients_park_a_card_and_an_approved_card_sends_once(world):
    out = _run(world, recipients="owner@example.com")
    assert out.status == "needs_approval"
    (card,) = _cards(world, "pending")
    assert world["mailer"].sent == []
    _decide(world, card["id"], "approved")
    _run(world, "2026-10-06", recipients="owner@example.com")
    _run(world, "2026-10-07", recipients="owner@example.com")
    assert len(world["mailer"].sent) == 1
    sent = world["mailer"].sent[0]
    assert sent["to"] == ["owner@example.com"]
    assert "Needs you" in sent["body"] and "$1,234.56" in sent["body"]


def test_a_rejected_card_sends_nothing(world):
    _run(world, recipients="owner@example.com")
    (card,) = _cards(world, "pending")
    _decide(world, card["id"], "rejected")
    _run(world, "2026-10-06", recipients="owner@example.com")
    assert world["mailer"].sent == []


def test_an_unattended_send_needs_no_card(world):
    _run(world, recipients="owner@example.com", unattended="send")
    assert _cards(world) == []
    assert len(world["mailer"].sent) == 1


def test_shadow_writes_and_sends_nothing(world):
    out = _run(world, shadow=True, recipients="owner@example.com", unattended="send")
    assert out.status == "ok"
    assert not (world["tmp"] / "briefs").exists()
    assert world["mailer"].sent == []


def test_week_label():
    from datetime import date

    assert brief_jobs.iso_week(date(2026, 10, 5)) == "2026-W41"
    assert brief_jobs.iso_week(date(2027, 1, 1)) == "2026-W53"


def test_render_is_pure(world):
    facts = {
        "as_of": "2026-10-05",
        "cards": [],
        "deadlines": [],
        "payables": {"rows": [], "total": "0.00"},
        "expenses": [],
        "runs": [],
        "sealed": None,
    }
    page = brief_jobs.render(facts, name="Demo Tenant Inc.")
    assert page.startswith("# Your week, 5 Oct 2026")
    assert "Nothing is waiting on you this week." in page


def test_a_scheduled_payable_shows_when_it_was_scheduled():
    facts = {
        "as_of": "2026-10-05",
        "cards": [],
        "deadlines": [],
        "payables": {
            "rows": [
                {
                    "vendor": "Acme",
                    "invoice_number": "9",
                    "amount": "520.00",
                    "status": "Scheduled",
                    "due_date": "2026-09-16",
                    "payment_date": "2026-09-30",
                }
            ],
            "total": "520.00",
        },
        "expenses": [],
        "runs": [],
        "sealed": None,
    }
    page = brief_jobs.render(facts, name="Demo")
    assert "Acme 9, $520.00, scheduled 30 Sep 2026" in page and "due 16 Sep" not in page
