"""The no-authority equivalence harness (#435).

A tenant with no ``authority.toml`` (the first tenant today: no kit, no
shape) must behave byte-identically after authority is wired into the queue.
This module drives every path #435 touches with that shape of config: the
queue CLI (approve, reject, a human-only card headless and at a terminal),
the weekly brief's send (unattended and through a card) and the deadlines
calendar write (unattended and through a card). It dumps what each leaves
behind (exit codes, output, cards, events, run results), with only clock
values and the scratch directory masked.

Run it against two builds and diff the output:

    uv run --project <tree> python -m tests.unit.authority_equivalence <dir> > out.json

``test_authority_equivalence.py`` holds the same dump, produced on main
before #435, to the code as it stands.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LANDING = REPO / "core/agents/ap/evals/fixtures/landing"
VOLATILE = re.compile(r"(_at|_started|^started|^finished|^ts|duration_ms|elapsed)$")
COMMIT = re.compile(r"commit: [0-9a-f]{7,40}")
RUN_KEY = re.compile(r"\.[0-9a-f]{16}\b")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?")

OBLIGATIONS = """
[[obligation]]
id = "permit"
title = "Work permit renewal"
who = "Jane"
due = 2026-11-20

[[obligation]]
id = "report"
title = "Quarterly report"
due = 2026-12-31
every = "3m"
lead_days = [14, 3]
"""


def first_tenant_shape(base: Path) -> Path:
    """The demo tenant as the first tenant is today: no authority.toml, no
    kit, no shape."""
    root = base / "tenants"
    shutil.copytree(REPO / "tenants" / "demo", root / "demo")
    (root / "demo" / "authority.toml").unlink(missing_ok=True)
    shutil.rmtree(root / "demo" / "kit", ignore_errors=True)
    toml = root / "demo" / "tenant.toml"
    toml.write_text(re.sub(r"(?m)^shape = .*\n", "", toml.read_text()), encoding="utf-8")
    return root


def _mask(obj, base: str, keys: bool):
    if isinstance(obj, dict):
        return {
            k: "<clock>" if (VOLATILE.search(k) or k == "commit") and v else _mask(v, base, keys)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask(v, base, keys) for v in obj]
    if isinstance(obj, str):
        text = COMMIT.sub("commit: <sha>", STAMP.sub("<clock>", obj.replace(base, "<base>")))
        return text if keys else RUN_KEY.sub(".<key>", text)
    return obj


class FakeMailer:
    def __init__(self):
        self.sent: list[dict] = []

    def send_mail(self, *, subject, body, to, attachments=()):
        self.sent.append({"subject": subject, "to": list(to), "body_lines": len(body.splitlines())})


class FakeCalendar:
    def __init__(self):
        self.calls: list[list] = []
        self._n = 0

    def create_all_day(self, *, subject, day, body, reminder_minutes, transaction_id, time_zone):
        self._n += 1
        self.calls.append(["create", subject, day.isoformat(), reminder_minutes])
        return f"E{self._n}"

    def update_all_day(self, event_id, *, subject, day, body, reminder_minutes, time_zone):
        self.calls.append(["update", event_id, subject])

    def delete(self, event_id):
        self.calls.append(["delete", event_id])


def _cli(argv: list[str], *, terminal: bool = False, typed: str = "") -> dict:
    from core.engine import cli

    out, err = io.StringIO(), io.StringIO()
    real_terminal, real_input = cli._operator_at_terminal, builtins.input
    cli._operator_at_terminal = lambda: terminal
    builtins.input = lambda prompt="": typed
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
    finally:
        cli._operator_at_terminal = real_terminal
        builtins.input = real_input
    return {"argv": argv[:4], "rc": rc, "out": out.getvalue(), "err": err.getvalue()}


def _ledger_state(ledger_dir: Path) -> dict:
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    with Ledger.open(resolve_ledger_root("demo", ledger_dir)) as ledger:
        cards = ledger.conn.execute(
            "SELECT id, idempotency_key, agent, action_type, params_json, status "
            "FROM approval_queue ORDER BY id"
        ).fetchall()
        events = ledger.conn.execute(
            "SELECT idempotency_key, agent, event_type, payload_json FROM events ORDER BY id"
        ).fetchall()
    return {
        "cards": [{**dict(r), "params_json": json.loads(r["params_json"])} for r in cards],
        "events": [
            {**dict(r), "payload_json": json.loads(r["payload_json"] or "null")} for r in events
        ],
    }


def _result(result) -> dict:
    return result.model_dump(mode="json")


def queue_scenario(base: Path) -> dict:
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    d = base / "queue"
    out: dict = {"steps": []}
    out["steps"].append(
        _cli(
            [
                "run",
                "demo",
                "ap",
                "intake",
                "--shadow",
                "--ledger-dir",
                str(d),
                "--param",
                f"landing_dir={LANDING}",
                "--param",
                "extractor=fixture",
            ]
        )  # fmt: skip
    )
    with Ledger.open(resolve_ledger_root("demo", d)) as ledger:
        rows = ledger.list_approvals("demo")
    plain = [r for r in rows if r["action_type"] != "ap.new_vendor_decision"]
    human = [r for r in rows if r["action_type"] == "ap.new_vendor_decision"]
    led = ["--ledger-dir", str(d)]
    if plain:
        out["steps"].append(_cli(["queue", "approve", "demo", "--id", str(plain[0]["id"]), *led]))
    if len(plain) > 1:
        out["steps"].append(_cli(["queue", "reject", "demo", "--id", str(plain[1]["id"]), *led]))
    if human:
        hid = str(human[0]["id"])
        out["steps"].append(_cli(["queue", "approve", "demo", "--id", hid, *led]))
        out["steps"].append(
            _cli(["queue", "approve", "demo", "--id", hid, *led], terminal=True, typed="nope")
        )
        out["steps"].append(
            _cli(["queue", "approve", "demo", "--id", hid, *led], terminal=True, typed=hid)
        )
    out["steps"].append(_cli(["queue", "approve", "demo", "--id", "9999", *led]))
    out["steps"].append(_cli(["queue", "list", "demo", *led]))
    out["ledger"] = _ledger_state(d)
    return out


def brief_scenario(base: Path, *, unattended: bool) -> dict:
    from core.agents.ap import store
    from core.agents.brief import jobs as brief_jobs
    from core.engine.runner import resolve_ledger_root, run
    from core.ledger import Ledger

    d = base / ("brief-unattended" if unattended else "brief-card")
    with Ledger.open(resolve_ledger_root("demo", d / "data")) as ledger:
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor="Acme Tooling",
            invoice_number="A-100",
            amount_cents=123456,
            due_date="2026-10-30",
        )
    ob = d / "obligations.toml"
    ob.write_text(OBLIGATIONS)
    mailer = FakeMailer()
    real = brief_jobs._send_client
    brief_jobs._send_client = lambda ctx: mailer
    params = {
        "today": "2026-10-05",
        "obligations_file": str(ob),
        "dir": str(d / "briefs"),
        "recipients": "owner@example.com",
    }
    if unattended:
        params["unattended"] = "send"
    runs = []
    steps = []
    try:
        runs.append(_result(run("demo", "brief", "weekly", params=params, ledger_dir=d / "data")))
        if not unattended:
            state = _ledger_state(d / "data")
            card = next(c for c in state["cards"] if c["action_type"] == brief_jobs.SEND_ACTION)
            steps.append(
                _cli(
                    [
                        "queue",
                        "approve",
                        "demo",
                        "--id",
                        str(card["id"]),
                        "--ledger-dir",
                        str(d / "data"),
                    ]
                )  # fmt: skip
            )
            params["today"] = "2026-10-06"
            runs.append(
                _result(run("demo", "brief", "weekly", params=params, ledger_dir=d / "data"))
            )
    finally:
        brief_jobs._send_client = real
    return {"runs": runs, "steps": steps, "sent": mailer.sent, "ledger": _ledger_state(d / "data")}


def calendar_scenario(base: Path, *, unattended: bool) -> dict:
    from core.agents.deadlines import calendar_sync as cs
    from core.engine.runner import run

    d = base / ("cal-unattended" if unattended else "cal-card")
    d.mkdir(parents=True)
    ob = d / "obligations.toml"
    ob.write_text(OBLIGATIONS)
    cal = FakeCalendar()
    real = cs._calendar_client
    cs._calendar_client = lambda ctx: cal
    params = {"obligations_file": str(ob), "today": "2026-10-04", "calendar": "graph"}
    if unattended:
        params["unattended"] = "calendar"
    runs, steps = [], []
    try:
        runs.append(
            _result(run("demo", "deadlines", "calendar", params=params, ledger_dir=d / "data"))
        )
        if not unattended:
            state = _ledger_state(d / "data")
            card = next(c for c in state["cards"] if c["action_type"] == cs.CAL_ACTION)
            steps.append(
                _cli(
                    [
                        "queue",
                        "approve",
                        "demo",
                        "--id",
                        str(card["id"]),
                        "--ledger-dir",
                        str(d / "data"),
                    ]
                )  # fmt: skip
            )
            runs.append(
                _result(run("demo", "deadlines", "calendar", params=params, ledger_dir=d / "data"))
            )
    finally:
        cs._calendar_client = real
    return {"runs": runs, "steps": steps, "calls": cal.calls, "ledger": _ledger_state(d / "data")}


def dump(base: Path, *, keys: bool = True) -> dict:
    """Every scenario, from an empty ``base``; masked for comparison. Run
    keys hash the scratch path, so ``keys=False`` masks them for a golden
    kept in the repo; two builds run at one path compare them exactly."""
    base.mkdir(parents=True, exist_ok=True)
    os.environ["ENGINE_TENANTS_ROOT"] = str(first_tenant_shape(base))
    os.environ.pop("ENGINE_LEDGER_ROOT", None)
    out = {
        "queue": queue_scenario(base),
        "brief_unattended": brief_scenario(base, unattended=True),
        "brief_card": brief_scenario(base, unattended=False),
        "calendar_unattended": calendar_scenario(base, unattended=True),
        "calendar_card": calendar_scenario(base, unattended=False),
    }
    return _mask(out, str(base), keys)


if __name__ == "__main__":
    print(json.dumps(dump(Path(sys.argv[1]).resolve()), indent=1, sort_keys=True))
