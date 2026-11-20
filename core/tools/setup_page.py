"""The box's end of a person's own setup (chunk 10; the owner's design of
2026-10-15: "no codes, no addresses: Face ID, then one question per
screen"; core/onboarding/person.py holds the steps and their words).

The doorkeeper's welcome page shows the screens and asks for the person's
own Face ID for a yes (the one that opened setup covers its yeses for
ten minutes in that browser, doorkeeper #39); it posts here on this same origin, and the
answers are JSON holding plain text the page puts in place with
``textContent``, never markup. Who is asking comes only from the
doorkeeper (localhost, the box's shared secret): a setup token it minted
after Face ID, for looking and for "not now"; and for a yes, a one-use
grant naming the person and the step, minted by their Face ID. A setup
buddy beside the person can tap "not now" but never yes.

Each step's yes does what the step needs and no more: the daily email's
yes asks the doorkeeper to send a hello to the address the ministry gave;
the receipt page's yes makes the person's standing home-screen link
(core/tools/receipts.py); mileage's yes makes their tracker token and the
tap that sets the app up (core/mileage/setup_link.py); the calendar's yes
makes their feed link (core/tools/calendar_feed.py); the AI step asks no
yes (2026-10-28): its button, Claude's own install link for the box's one
connector, is the yes, and it's done at their AI's first question to Tim.

The reviewer's side (docs/screens/README.md, R2 and R3; screens chunk 3,
2026-11-16): to someone a waiting expense plan asks to adopt it, setup
first says what the person they look over is asking (the role's yes writes
only the answer), then shows the plan. The plan's button is the plan card's
own yes link (core/tools/decide.py), so its Face ID is asked right then and
never borrowed from setup's; a setup grant never adopts it. The step is
done once the plan is in force.
Where Tim's own voice is on (the ministry's file names a tier for
``talk``; Standalone Tim, card T2, 2026-11-17), the AI step is "Where do you
want to talk to me?": offered to everyone, Tim's own page first and the
Claude or ChatGPT link as the "also", and done at her first word to Tim on
his page (core/tools/talk_page.py) as at her AI's first question. The
home-screen step points at his page, and is done when it opens from her
home screen. Where the voice is off, every screen is as it was
(tests/unit/test_voice_off_pinned.py).
A step is done only once Tim sees it work. Standard library only, but for the QR code.
"""

from __future__ import annotations

import json
import threading
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs

from ..agents.policy.jobs import PLAN_CARD
from ..engine.config import load_tenant, tenant_dir
from ..mileage import setup_link
from ..mileage.trackers import add_tracker
from ..onboarding import OnboardingError
from ..onboarding.person import Facts, SetupBook, at_sign_up, next_step, offered, words
from ..talk import voice_on, voice_on_for
from . import calendar_feed as cal
from . import receipts as rcpt
from .talk_page import TALK_PATH
from .viewer import display_name, first_name

NEXT_PATH = "/setup/next"
YES_PATH = "/setup/yes"
LATER_PATH = "/setup/later"
AGAIN_PATH = "/setup/again"
SEEN_PATH = "/setup/seen"
RESEND_PATH = "/setup/resend"
PATHS = frozenset({NEXT_PATH, YES_PATH, LATER_PATH, AGAIN_PATH, SEEN_PATH, RESEND_PATH})
MAX_FORM = 2048

NEXT_WORDS = (
    "Next, add Tim to Claude or ChatGPT. When it asks you to sign in, use the same Face ID or "
    "fingerprint. In Claude, then set Tim's tools to Always allow (Customize, then Connectors, "
    "then Tim), "
    "so Tim doesn't stop to ask before each step."
)
_GONE = "This page has timed out. Open your welcome page again, or ask Tim for a new link."
_NO_YES = "That yes didn't go through. Tap the button and use Face ID again."


class SetupDesk(Protocol):
    """The doorkeeper, as setup needs it (core/tools/witness_desk.py)."""

    def setup_person(self, token: str) -> str | None: ...

    def spend_setup(self, grant: str) -> tuple[str, str] | None: ...

    def hello(self, person: str) -> bool: ...

    def heard(self, person: str) -> bool: ...


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SetupPage:
    def __init__(
        self,
        tenant: str,
        *,
        tenants_root: str | Path | None,
        desk: SetupDesk,
        origin: str,
        ministry: str = "",
        connector: str = "",
        tools_for: Callable[[str], Any] | None = None,
        now: Callable[[], str] = _now,
    ) -> None:
        self.tenant = tenant
        self.tools_for = tools_for
        self.root = Path(tenants_root) if tenants_root else None
        self.folder = tenant_dir(tenant, tenants_root=self.root)
        self.desk = desk
        self.origin = origin
        self.ministry = ministry
        self.connector = connector or origin + "/mcp"
        self.now = now
        self.book = SetupBook(self.folder)
        self._lock = threading.Lock()
        # The keys a person's yes made, held for this run of the box so a
        # reload shows the same link; the files keep only their hashes.
        self._made: dict[tuple[str, str], str] = {}

    # ---- who, and what fits them

    def _facts(self, person: str) -> Facts:
        email = rcpt.person_entry(self.folder, person).get("email", "")
        return Facts(
            email=email if isinstance(email, str) else "",
            files_expenses=bool(rcpt.filer_name(self.tenant, self.root, person)),
            adopts_plan=self._adopts(person),
        )

    # ---- the reviewer's side: the expense plan

    def _plan(self, person: str) -> tuple[Any, dict] | None:
        """(their tools, the newest plan card) when one is waiting or in
        force, on a box that decides cards; else None."""
        if self.tools_for is None:
            return None
        t = self.tools_for(person)
        if getattr(t, "decider", None) is None or not t.decider.offered():
            return None
        rows = t._rows(
            "SELECT id, status, params_json FROM approval_queue WHERE tenant = ? "
            "AND action_type = ? AND status IN ('pending', 'approved') ORDER BY id DESC LIMIT 1",
            (t.tenant, PLAN_CARD),
        )
        return (t, dict(rows[0])) if rows else None

    def _adopts(self, person: str) -> bool:
        """The plan waits on this person's yes, or they began its screens."""
        answers = self.book.answers(person)
        if answers.get("role") or answers.get("plan"):
            return True
        found = self._plan(person)
        if found is None:
            return False
        t, row = found
        return row["status"] == "pending" and t.decider.may_tap(t, int(row["id"]))

    def _adopted(self, person: str) -> bool:
        found = self._plan(person)
        return found is not None and found[1]["status"] == "approved"

    def _who(self, person: str) -> str:
        """Whoever the plan covers, by first name: "Daniel"."""
        found = self._plan(person)
        params = json.loads(found[1]["params_json"] or "{}") if found else {}
        covers = {c for c in str(params.get("covers", "")).split(",") if c}
        try:
            with (self.folder / "authority.toml").open("rb") as handle:
                people = tomllib.load(handle).get("people", {})
        except (OSError, tomllib.TOMLDecodeError):
            people = {}
        names = [
            first_name(display_name(pid))
            for pid, entry in sorted(people.items())
            if pid != person
            and isinstance(entry, dict)
            and (pid in covers or covers & set(entry.get("roles", [])))
        ]
        return " and ".join(names) if 0 < len(names) <= 2 else "your ministry's workers"

    def _plan_words(self, person: str) -> dict[str, Any]:
        """The plan's rules, in its own words, its whole text, and the link
        whose Face ID adopts it."""
        found = self._plan(person)
        if found is None or found[1]["status"] != "pending":
            return {"rules": [], "whole": "", "adopt": ""}
        t, row = found
        params = json.loads(row["params_json"] or "{}")
        folder = self.folder / "rules"
        try:
            with (folder / Path(str(params.get("rules_file", ""))).name).open("rb") as handle:
                held = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            held = {}
        rules = [str(r["says"]) for r in held.get("rule", []) if r.get("says")]
        copy = held.get("copy", {})
        try:
            whole = (folder / Path(str(copy.get("file", ""))).name).read_text(encoding="utf-8")
        except (OSError, IsADirectoryError):
            whole = ""
        out = t.decider.decide(t, {"card": int(row["id"]), "decision": "approve"}, tap=True)
        return {"rules": rules, "whole": whole, "adopt": str(out.get("link") or "")}

    def _steps(self, person: str) -> tuple[tuple[str, ...], Facts]:
        facts = self._facts(person)
        cfg = load_tenant(self.tenant, tenants_root=self.root)
        return offered(facts, cfg.setup.offer, voice=voice_on(cfg.llm)), facts

    def _voice(self) -> bool:
        """Tim's own voice is on for the ministry (card T2): his page comes
        first and is the one on the home screen. Off, nothing changes."""
        return voice_on_for(self.tenant, self.root)

    # ---- what a yes makes

    def _key(self, person: str, step: str) -> str:
        with self._lock:
            made = self._made.get((person, step))
            if made is None:
                if step == "home_screen":
                    made = rcpt.add_home_link(self.folder, person)
                elif step == "calendar":
                    made = cal.add_calendar_link(self.folder, person)
                else:
                    made = add_tracker(self.folder, person, only_one=True)
                self._made[(person, step)] = made
            return made

    def _extras(self, person: str, step: str, said: dict, voice: bool) -> dict[str, Any]:
        if voice and step == "home_screen":  # Tim's own page is the one there (card T2)
            return {"open": self.origin + TALK_PATH, "open_label": said["open"]}
        if voice and step == "ai_connector":  # right here first, the AI as "also"
            return {
                "talk": self.origin + TALK_PATH,
                "talk_label": said["talk"],
                "also": said["also"],
                "connector": self.connector,
            }
        if step == "home_screen":
            return {"open": self.origin + rcpt.HOME_PATH + self._key(person, step)}
        if step == "mileage":
            token = self._key(person, step)
            return {
                "app_link": setup_link.app_link(self.origin, token),
                "start_link": setup_link.START_LINK,
                "stores": dict(setup_link.STORES),
                "qr": setup_link.qr_rows(setup_link.qr_text(self.origin, token)),
            }
        if step == "calendar":
            return cal.links(self.origin, self._key(person, step))
        if step == "ai_connector":
            return {"connector": self.connector}
        return {}

    # ---- the screen

    def screen(self, person: str) -> dict[str, Any]:
        steps, facts = self._steps(person)
        answers = self.book.answers(person)
        if (
            "daily_email" in steps
            and not answers.get("daily_email", {}).get("yes")
            and self.desk.heard(person)
        ):
            # Their welcome came by mail to that address and they opened it
            # (the doorkeeper heard it): the email step needs no hello.
            self.book.proved(person, "daily_email", how="welcome", now=self.now())
            answers = self.book.answers(person)
        if "plan" in steps and not answers.get("plan", {}).get("seen") and self._adopted(person):
            self.book.saw(person, "plan", now=self.now())
            answers = self.book.answers(person)
        name = display_name(person).split(" ")[0]
        voice = self._voice()
        walk = at_sign_up(answers, steps)
        if answers.get("role", {}).get("later"):
            walk = tuple(s for s in walk if s != "plan")  # "I can't take this on"
        found = next_step(answers, walk)
        if found is None:
            return {
                "stage": "done",
                "name": name,
                "set": [
                    words(s, voice=voice)["done"] for s in steps if answers.get(s, {}).get("seen")
                ],
                "not_yet": [
                    {"step": s, "name": words(s, voice=voice)["name"]}
                    for s in steps
                    if not answers.get(s, {}).get("seen")
                ],
                # The AI is a step of its own where the ministry offers it.
                "next": "" if "ai_connector" in steps else NEXT_WORDS,
            }
        step, stage = found
        who = self._who(person) if step in ("role", "plan") else ""
        said = words(step, email=facts.email, who=who, voice=voice)
        asked = [s for s in walk if not answers.get(s, {}).get("proved")]
        out: dict[str, Any] = {
            "step": step,
            "stage": stage,
            "name": name,
            "n": asked.index(step) + 1,
            "of": len(asked),
            "ask": said["ask"],
            "help": said["help"],
            "later": said["later"],
        }
        if said["points_heading"]:
            out.update(points_heading=said["points_heading"], points=said["points"])
        if step == "plan":
            plan = self._plan_words(person)
            out.update(points=plan["rules"], whole=plan["whole"], adopt=plan["adopt"])
            out["yes"] = said["yes"]
        if stage == "ask":
            out["yes"] = said["yes"]
        else:
            out.update(
                waiting=said["waiting"],
                done=said["done"],
                **self._extras(person, step, said, voice),
            )
        return out

    def unfinished(self, person: str) -> bool:
        """Whether ``person`` still has a setup step ahead: setup's own next
        screen is a step, not the last one (Tim's step-up rule, card V3).
        The same reading the screen makes, side effects and all, so it never
        disagrees with what her setup page would show."""
        return self.screen(person).get("stage") != "done"

    # ---- the posts

    def answer(self, path: str, headers: Any, raw: bytes) -> tuple[int, dict]:
        """One post from the welcome page: (status, body)."""
        given = headers.get("Origin")
        if given and given != self.origin:
            return 403, {"say": "That didn't come from Tim's page."}
        if len(raw) > MAX_FORM:
            return 413, {"say": _GONE}
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        path = path.split("?")[0]
        if path == YES_PATH:
            return self._yes(form.get("grant", ""))
        person = self.desk.setup_person(form.get("token", "")) if form.get("token") else None
        if person is None:
            return 401, {"say": _GONE}
        try:
            if path == NEXT_PATH:
                return 200, self.screen(person)
            step = form.get("step", "")
            steps, _facts = self._steps(person)
            if step not in steps:
                return 400, {"say": "That isn't part of your setup."}
            if path in (LATER_PATH, AGAIN_PATH):
                said = "later" if path == LATER_PATH else "again"
                self.book.record(person, step, said, now=self.now())
                return 200, self.screen(person)
            if path == RESEND_PATH:
                return 200, {"sent": step == "daily_email" and self.desk.hello(person)}
            return 200, {"seen": self._seen(person, step)}
        except OnboardingError:
            return 400, {"say": "That isn't part of your setup."}

    def _yes(self, grant: str) -> tuple[int, dict]:
        found = self.desk.spend_setup(grant) if grant else None
        if found is None:
            return 400, {"say": _NO_YES}
        person, step = found
        if step not in self._steps(person)[0]:
            return 400, {"say": "That isn't part of your setup."}
        if step == "plan":  # adopted only on its own yes page, by a Face ID asked then
            return 400, {"say": _NO_YES}
        self.book.record(person, step, "yes", now=self.now())
        if step == "role":  # "I'll do it": nothing to see work but the answer
            self.book.saw(person, step, now=self.now())
        elif step == "daily_email":
            self.desk.hello(person)
        elif step != "ai_connector":
            with self._lock:
                self._made.pop((person, step), None)  # a fresh yes, a fresh key
        return 200, self.screen(person)

    def _seen(self, person: str, step: str) -> bool:
        if step == "daily_email" and self.desk.heard(person):
            self.book.saw(person, step, now=self.now())
        if step == "plan" and self._adopted(person):
            self.book.saw(person, step, now=self.now())
        return bool(self.book.answers(person).get(step, {}).get("seen"))


def page_for(
    desk: Any,
    *,
    tenant: str,
    tenants_root: Any,
    origin: str,
    ministry: str,
    connector: str = "",
    tools_for: Callable[[str], Any] | None = None,
):
    """Setup, on a box whose doorkeeper vouches for people."""
    if desk is None:
        return None
    return SetupPage(
        tenant,
        tenants_root=tenants_root,
        desk=desk,
        origin=origin,
        ministry=ministry,
        connector=connector,
        tools_for=tools_for,
    )


__all__ = ["PATHS", "SetupDesk", "SetupPage", "page_for"]
