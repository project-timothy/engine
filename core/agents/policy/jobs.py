"""Policy agent jobs: propose (chunk 5, board item t-policy-rules).

A ministry's handbook becomes rules under ``<tenant>/rules/`` (core/policy).
Whoever drafts them, a model or a person, nothing drafted is in force:
``propose`` raises one card per rule, keyed to the rule's exact words and
amounts (its fingerprint), and a rule is in force only while an approved
card carries that fingerprint. New words are a new card, which retires the
old one still waiting; an approval given to the old words never covers the
new ones.

Deciding the card is confirming an authority change (``confirm:authority``),
so the floor holds: no agent confirms one, and a card that commits no money
always takes the person's own Face ID through the door. During the alpha
the owner approves every rule (2026-10-14); from beta, the ministry's own
structure does, by the same grant.

An accountable plan (a rules file with ``plan = true``, chunk 3) is one
card, not one per rule: a single ``policy.plan`` card carries every rule's
fingerprint, and its yes puts them all in force together. Only the role the
plan names (``adopted_by``) decides it, and never anyone the plan covers,
even where the ministry lets people approve their own (the floor in
core/authority refuses them by ``covers``).

``ask`` (chunk 7, board item t-ask-first): before the spend, a person asks
for the OK a rule in force says they need. Code works out which rules ask
for one at that amount, and raises one ``policy.ask`` card per role those
rules name (none named: whoever may approve a purchase for them). Only the
role a rule names decides its card (core/authority, ``Request.role``). Once
approved, the month card counts that yes for one purchase of that kind, at
or under that amount, from the day it was asked (core/policy, ``with_oks``).

``watch`` (chunk 8, the owner's ask of 2026-10-15): every morning and
whenever someone runs it, read the copy each rules file names (``[copy]``,
a file under ``rules/`` or an https url), hash it, and find every rule's
quote in it. A copy unlike the one the rules were drafted from (its
``sha``), or one missing a rule's words, raises one ``policy.copy`` card
to whoever approves rules, saying which rules still read word for word
and which do not. Nothing goes in or out of force on a copy: a rule stays
exactly as approved until its file changes and its own card gets a yes.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tomllib
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...authority import CardRule, Policy, card_request, evaluate
from ...engine.clock import local_today
from ...engine.contracts import ApprovalSpec, JobContext, JobHandler, JobOutput
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from ...ledger import Ledger
from ...policy import (
    MONTH,
    Clock,
    Layer,
    Ok,
    Pay,
    PolicyError,
    Rule,
    Rulebook,
    Spent,
    check_month,
    clock,
    find_quote,
    pay_for,
)
from ...policy.copies import CopyUnreadable, read_copy
from ..expenses.categories import canonical, from_words

RULE_CARD = "policy.rule"
PLAN_CARD = "policy.plan"
ASK_CARD = "policy.ask"
COPY_CARD = "policy.copy"


def _rule_cards(ledger: Ledger, tenant: str, action: str = RULE_CARD) -> list[dict]:
    rows = ledger.conn.execute(
        "SELECT id, status, idempotency_key, params_json FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? ORDER BY id",
        (tenant, action),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "status": r["status"],
            "key": r["idempotency_key"],
            "params": json.loads(r["params_json"] or "{}"),
        }
        for r in rows
    ]


def approved_rule_shas(conn: sqlite3.Connection, tenant: str) -> set[str]:
    """The fingerprints a person said yes to: an approved ``policy.rule``
    card's one, and every one an adopted plan (``policy.plan``) carries."""
    rows = conn.execute(
        "SELECT action_type, params_json FROM approval_queue "
        "WHERE tenant = ? AND action_type IN (?, ?) AND status = 'approved'",
        (tenant, RULE_CARD, PLAN_CARD),
    ).fetchall()
    shas: set[str] = set()
    for action, raw in rows:
        params = json.loads(raw or "{}")
        if action == RULE_CARD:
            shas.add(str(params.get("sha", "")))
        else:
            shas.update(s for s in str(params.get("shas", "")).split(",") if s)
    return shas


def rules_in_force(ledger: Ledger, tenant: str, book: Rulebook | None) -> list[Rule]:
    """The rules a person said yes to, word for word, highest layer first."""
    if book is None:
        return []
    return book.in_force(approved_rule_shas(ledger.conn, tenant))


def clock_in_force(
    conn: sqlite3.Connection, tenant: str, book: Rulebook | None, policy: Policy | None, person: str
) -> Clock:
    """The cash clock for one person (named or by id): what an adopted plan
    says, else the days Tim always said. Shared by the month card, Tim's
    cash summary, and the nudge to count (chunk 3)."""
    if book is None:
        return Clock()
    approved = approved_rule_shas(conn, tenant)
    who = re.sub(r"[^a-z0-9]+", "-", person.casefold()).strip("-")
    held = policy.people.get(who) if policy is not None else None
    in_force = book.in_force(approved)
    return clock(in_force, person=who, roles=held.roles if held else ())


def pay_in_force(
    conn: sqlite3.Connection, tenant: str, book: Rulebook | None, person: str, month: str
) -> Pay | None:
    """What ``person`` (their id) is paid in ``month`` (YYYY-MM) under the
    board's adopted resolutions; None when none covers it (c34). Shared by
    pay day, the clocks, and the month card."""
    if book is None:
        return None
    return pay_for(book.in_force(approved_rule_shas(conn, tenant)), person, month)


def _book(ctx: JobContext) -> tuple[Rulebook | None, str]:
    authority = ctx.authority
    if authority is None:
        return None, ""
    return authority.rules, authority.rules_error


def _key(ctx: JobContext) -> str:
    key = RunKey(ctx, "policy-propose")
    book, error = _book(ctx)
    key.value("rules", sorted(r.sha for r in book.rules) if book else [])
    key.value("rules_error", error)
    key.value("cards", [(c["id"], c["status"]) for c in _rule_cards(ctx.ledger, ctx.tenant_slug)])
    plans = _rule_cards(ctx.ledger, ctx.tenant_slug, PLAN_CARD)
    planned = [layer for layer in book.layers if layer.plan] if book else []
    if plans or planned:
        # A ministry with no plan keeps the key it had.
        key.value("plans", [(c["id"], c["status"]) for c in plans])
        key.value("adopters", [(layer.file, layer.adopted_by) for layer in planned])
    return key.digest()


def _card_key(rule: Rule) -> str:
    return f"rule:{rule.layer}:{rule.id}:{rule.sha[:16]}"


def _propose_run(ctx: JobContext) -> JobOutput:
    book, error = _book(ctx)
    if error:
        return JobOutput(
            status="error",
            summary="policy propose: the rules did not load; nothing was carded",
            anomalies=[Anomaly(code="policy.rules_unreadable", detail=error)],
        )
    if book is None or not book.rules:
        return JobOutput(status="ok", summary="policy propose: no rules for this ministry")
    cards = _rule_cards(ctx.ledger, ctx.tenant_slug)
    carded = {c["params"].get("sha") for c in cards}
    prefix = f"apr:{ctx.tenant_slug}:policy:{RULE_CARD}:"
    approvals = []
    planned = [layer for layer in book.layers if layer.plan]
    for layer in planned:
        card = _plan_card(ctx, layer)
        if card is not None:
            approvals.append(card)
    in_plans = {r.sha for layer in planned for r in layer.rules}
    for rule in book.rules:
        if rule.sha in carded or rule.sha in in_plans:
            continue
        older = [
            c["key"][len(prefix) :]
            for c in cards
            if c["status"] == "pending"
            and c["key"].startswith(prefix)
            and c["params"].get("rule") == rule.id
            and c["params"].get("layer") == rule.layer
        ]
        # A draft that takes another rule's place says which (t-rule-drafting).
        instead = next(
            (
                r
                for layer in book.layers
                if rule in layer.rules
                for r in layer.rules
                if rule.replaces and r.id == rule.replaces
            ),
            None,
        )
        place = f' in place of "{instead.says}"' if instead else ""
        approvals.append(
            ApprovalSpec(
                key=_card_key(rule),
                action_type=RULE_CARD,
                params={
                    # How the door names the card (decide.card_words).
                    "what": f'the rule "{rule.says}" from {rule.cite()}{place}',
                    "rule": rule.id,
                    "layer": rule.layer,
                    "says": rule.says,
                    "from": rule.cite(),
                    "quote": rule.source.quote,
                    "sha": rule.sha,
                },
                reason="a drafted rule is in force only after a person's yes to its exact words",
                supersedes_keys=older,
            )
        )
    plans = _rule_cards(ctx.ledger, ctx.tenant_slug, PLAN_CARD)
    waiting = sum(1 for c in cards + plans if c["status"] == "pending") + len(approvals)
    in_force = len(rules_in_force(ctx.ledger, ctx.tenant_slug, book))
    summary = (
        f"policy propose: {len(book.rules)} rule(s), {in_force} in force; "
        f"{len(approvals)} new card(s)"
    )
    if ctx.shadow:
        return JobOutput(status="ok", summary=summary + " (shadow: none parked)")
    return JobOutput(
        status="needs_approval" if approvals else "ok",
        summary=summary + (f", {waiting} waiting" if waiting else ""),
        approvals=approvals,
    )


def _plan_card(ctx: JobContext, layer: Layer) -> ApprovalSpec | None:
    """One card that adopts a whole plan, or None when this wording of it
    already has one. New words are a new card; one still waiting for the
    old words retires."""
    shas = sorted(r.sha for r in layer.rules)
    whole = hashlib.sha256(",".join(shas).encode()).hexdigest()
    cards = _rule_cards(ctx.ledger, ctx.tenant_slug, PLAN_CARD)
    if any(c["params"].get("plan_sha") == whole for c in cards):
        return None
    prefix = f"apr:{ctx.tenant_slug}:policy:{PLAN_CARD}:"
    pays = any(r.pays for r in layer.rules)
    stays = (
        "It goes in force on one yes from a director with no family tie to the person it pays"
        if pays
        else f"All {len(layer.rules)} go in force together on this one yes"
    )
    kin = {"kin": "yes"} if pays else {}  # family of whom it covers abstains (c34)
    return ApprovalSpec(
        key=f"plan:{layer.layer}:{layer.file}:{whole[:16]}",
        action_type=PLAN_CARD,
        params={
            # How the door names the card (decide.card_words), then each
            # rule on its own line (decide.plain_words).
            "what": f"the {layer.document}",
            "lines": "; ".join(f"{r.says.rstrip('.')} ({r.cite()})" for r in layer.rules),
            "stays": stays,
            **kin,
            "document": layer.document,
            "layer": layer.layer,
            "rules_file": layer.file,
            "adopted_by": layer.adopted_by,
            "covers": ",".join(layer.covers),
            "shas": ",".join(shas),
            "plan_sha": whole,
        },
        reason="a plan is in force only after the yes of someone it does not cover",
        supersedes_keys=[
            c["key"][len(prefix) :]
            for c in cards
            if c["status"] == "pending"
            and c["key"].startswith(prefix)
            and c["params"].get("rules_file") == layer.file
        ],
    )


# ---- ask: the OK a rule says to get before buying (chunk 7) ----------------------


def _display(person: str) -> str:
    """``linda-park`` reads as "Linda Park" (core/tools/viewer.display_name;
    the agent does not import the door)."""
    words = [w for w in person.split("-") if w]
    return " ".join(
        w if w == "and" and 0 < i < len(words) - 1 else w.capitalize() for i, w in enumerate(words)
    )


def _cents(raw: object) -> int | None:
    text = str(raw or "").replace("$", "").replace(",", "").strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return int(value * 100) if value > 0 else None


def _ask_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "policy-ask")
    for name in ("person", "what", "kind", "amount", "today"):
        key.param(name)
    book, error = _book(ctx)
    key.value("rules", sorted(r.sha for r in rules_in_force(ctx.ledger, ctx.tenant_slug, book)))
    key.value("rules_error", error)
    key.config("expenses.category_accounts")
    return key.digest()


def _ask_run(ctx: JobContext) -> JobOutput:
    """One purchase, before it is made: which rules in force ask for an OK at
    this amount, and a card to whoever each names. Nothing is decided here."""
    book, error = _book(ctx)
    if error or ctx.authority is None:
        return JobOutput(status="error", summary="policy ask: the rules did not load")
    person = str(ctx.params.get("person") or "")
    held = ctx.authority.policy.people.get(person)
    if held is None or held.kind != "person":
        return JobOutput(status="error", summary=f"policy ask: {person!r} is not a person here")
    what = " ".join(str(ctx.params.get("what") or "").split())
    cents = _cents(ctx.params.get("amount"))
    listed = list(ctx.tenant.expenses.category_accounts)
    kind = canonical(str(ctx.params.get("kind") or ""), listed) or from_words(what, listed)
    if not what or cents is None or not kind:
        return JobOutput(status="error", summary="policy ask: needs what, an amount, and a kind")
    in_force = rules_in_force(ctx.ledger, ctx.tenant_slug, book)
    line = Spent("ask", what, kind, cents, receipt=False)
    found = check_month(in_force, [line], person=person, roles=held.roles)
    asking = [f.rule for f in found if f.rule.needs.approver or f.rule.needs.before]
    if not asking:
        return JobOutput(status="ok", summary="policy ask: no rule in force asks for an OK first")
    today = str(ctx.params.get("today") or local_today(ctx.tenant.identity.timezone))
    by_role: dict[str, list[Rule]] = {}
    for rule in asking:
        by_role.setdefault(rule.needs.approver, []).append(rule)
    policy = ctx.authority.policy
    approvals: list[ApprovalSpec] = []
    nobody: list[str] = []
    for role, rules in by_role.items():
        params = {
            # How the door names the card (decide.card_words).
            "what": f"an OK before buying {what}",
            "person": _display(person),
            "kind": kind,
            "amount": f"{Decimal(cents) / 100:.2f}",
            "approver_role": role,
            "asked_on": today,
            "rules": "; ".join(f"{r.says} ({r.cite()})" for r in rules),
            "shas": ",".join(r.sha for r in rules),
        }
        rule = CARD_AUTHORITY[ASK_CARD]
        deciders = [
            p
            for p, who in policy.people.items()
            if who.kind == "person"
            and p != person
            and evaluate(policy, card_request(rule, p, params, policy)).allowed
        ]
        if not deciders:
            nobody.append(role or "an approver")
            continue
        params["asked_of"] = ", ".join(_display(p) for p in deciders)
        digest = hashlib.sha256(f"{what}|{kind}|{cents}".encode()).hexdigest()[:12]
        approvals.append(
            ApprovalSpec(
                key=f"ask:{person}:{today}:{role or 'any'}:{digest}",
                action_type=ASK_CARD,
                params=params,
                reason="a rule in force asks for this OK before the purchase",
            )
        )
    summary = f"policy ask: {len(approvals)} card(s) for {kind}, ${cents / 100:,.2f}"
    anomalies = [
        Anomaly(code="policy.ask_nobody", detail=f"no one but the asker holds {r}") for r in nobody
    ]
    if ctx.shadow or not approvals:
        return JobOutput(status="ok", summary=summary, anomalies=anomalies)
    return JobOutput(
        status="needs_approval", summary=summary, approvals=approvals, anomalies=anomalies
    )


def approved_oks(conn: sqlite3.Connection, tenant: str, person: str) -> list[Ok]:
    """The yeses a person was given before the spend (approved ``policy.ask``
    cards), for the month card (core/policy.with_oks)."""
    rows = conn.execute(
        "SELECT params_json FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? AND status = 'approved' ORDER BY id",
        (tenant, ASK_CARD),
    ).fetchall()
    oks = []
    for (raw,) in rows:
        p = json.loads(raw or "{}")
        cents = _cents(p.get("amount"))
        if p.get("person") != _display(person) or cents is None:
            continue
        by = _display(str(p.get("decided_by") or "")) or str(p.get("asked_of") or "")
        shas = tuple(s for s in str(p.get("shas") or "").split(",") if s)
        oks.append(Ok(by, str(p.get("kind") or ""), cents, str(p.get("asked_on") or ""), shas))
    return oks


# ---- watch: is the handbook the rules were read from still the same (chunk 8) ----


def _copy_cards(ledger: Ledger, tenant: str) -> list[dict]:
    rows = ledger.conn.execute(
        "SELECT status, idempotency_key, params_json FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? ORDER BY id",
        (tenant, COPY_CARD),
    ).fetchall()
    return [
        {
            "status": r["status"],
            "key": r["idempotency_key"],
            "params": json.loads(r["params_json"] or "{}"),
        }
        for r in rows
    ]


def _watch_key(ctx: JobContext) -> str:
    # Every look is a fresh look (the stamp): a copy at a url may change with
    # nothing here changing, and a card already raised for a copy is never
    # raised again. A file's own bytes, and ``--param at=...`` for a forced
    # look, move the key inside the same second too.
    key = RunKey(ctx, "policy-watch").stamp()
    key.param("at")
    book, error = _book(ctx)
    key.value("rules_error", error)
    for layer in book.layers if book else ():
        if layer.copy and layer.copy.path and Path(layer.copy.path).is_file():
            key.file(layer.copy.path, label=f"copy:{layer.file}")
    return key.digest()


def _watch_run(ctx: JobContext) -> JobOutput:
    """Each rules file's copy, read and checked word for word. Nothing is
    decided here, and no rule goes in or out of force."""
    book, error = _book(ctx)
    if error:
        return JobOutput(
            status="error",
            summary="policy watch: the rules did not load; no copy was read",
            anomalies=[Anomaly(code="policy.rules_unreadable", detail=error)],
        )
    watched = [(layer, layer.copy) for layer in (book.layers if book else ()) if layer.copy]
    if not watched:
        return JobOutput(status="ok", summary="policy watch: no rules file names its copy")
    carded = {c["params"].get("key") for c in _copy_cards(ctx.ledger, ctx.tenant_slug)}
    prefix = f"apr:{ctx.tenant_slug}:policy:{COPY_CARD}:"
    pending = [c for c in _copy_cards(ctx.ledger, ctx.tenant_slug) if c["status"] == "pending"]
    approvals: list[ApprovalSpec] = []
    anomalies: list[Anomaly] = []
    new = gone_total = 0
    for layer, copy in watched:
        try:
            sha, pages = read_copy(layer, copy)
        except CopyUnreadable as exc:
            anomalies.append(Anomaly(code="policy.copy_unreadable", detail=str(exc)))
            continue
        still = [r for r in layer.rules if find_quote(r.source.quote, pages) is not None]
        gone = [r for r in layer.rules if r not in still]
        changed = bool(copy.sha) and sha != copy.sha
        new += changed
        gone_total += len(gone)
        if not changed and not gone:
            continue
        card = f"copy:{layer.layer}:{layer.file}:{sha[:16]}"
        if card in carded:
            continue
        what = f"a new copy of {layer.document}" if changed else f"the copy of {layer.document}"
        lines = [f"Not in this copy: {r.says} ({r.cite()})" for r in gone] + [
            f"Still word for word: {r.says} ({r.cite()})" for r in still
        ]
        # What a yes leaves alone, said on the card and once it is done
        # (the owner's phone run, 2026-10-15: "did the meal rule survive?").
        stays = "No rule changes with this yes; every rule still reads as it was approved"
        if gone:
            stays += (
                ", so "
                + " and ".join(f'"{r.says.rstrip(".")}"' for r in gone)
                + " still counts until its new words get their own yes"
            )
        approvals.append(
            ApprovalSpec(
                key=card,
                action_type=COPY_CARD,
                params={
                    # How the door names the card (decide.card_words), then
                    # each rule on its own line (decide.plain_words).
                    "what": what,
                    "lines": "; ".join(lines),
                    "stays": stays,
                    "key": card,
                    "document": layer.document,
                    "layer": layer.layer,
                    "rules_file": layer.file,
                    "from": copy.url or copy.file,
                    "sha": sha,
                    "drafted_from": copy.sha,
                    "gone": ",".join(r.id for r in gone),
                    "still": ",".join(r.id for r in still),
                },
                reason=(
                    "the copy the rules were read from changed"
                    if changed
                    else "a rule's words are not in the copy it cites"
                ),
                supersedes_keys=[
                    c["key"][len(prefix) :]
                    for c in pending
                    if c["key"].startswith(prefix)
                    and c["params"].get("rules_file") == layer.file
                    and c["params"].get("layer") == layer.layer
                ],
            )
        )
    summary = (
        f"policy watch: {len(watched)} copy(ies), {new} new, "
        f"{gone_total} rule(s) whose words are not in theirs; {len(approvals)} new card(s)"
    )
    if ctx.shadow or not approvals:
        return JobOutput(status="ok", summary=summary, anomalies=anomalies)
    return JobOutput(
        status="needs_approval", summary=summary, approvals=approvals, anomalies=anomalies
    )


# ---- draft: a rule from a new copy, written in (t-rule-drafting) ---------------------

DRAFT_RECORD = "policy.rule_drafted"


def _draft_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "policy-draft")
    for name in ("person", "file", "rule"):
        key.param(name)
    book, error = _book(ctx)
    key.value("rules_error", error)
    for layer in book.layers if book else ():
        if layer.file == ctx.params.get("file") and layer.copy and layer.copy.path:
            if Path(layer.copy.path).is_file():
                key.file(layer.copy.path, label=f"copy:{layer.file}")
    key.config("expenses.category_accounts")
    return key.digest()


def _draft_run(ctx: JobContext) -> JobOutput:
    """One drafted rule into its rules file, after core/policy/drafts.py
    checks it against the new copy; nothing goes in force here. Only someone
    who decides the ministry's rules drafts one."""
    from ...authority.routing import can_decide
    from ...engine.config import tenant_dir
    from ...policy.drafts import draft, rules_file

    book, error = _book(ctx)
    policy = ctx.authority.policy if ctx.authority is not None else None
    person, file = str(ctx.params.get("person", "")), str(ctx.params.get("file", ""))
    if error or book is None or policy is None:
        return JobOutput(status="error", summary=f"policy draft: {error or 'no rules here'}")
    if not can_decide(policy, CARD_AUTHORITY[RULE_CARD], person):
        return JobOutput(status="error", summary="policy draft: only who decides the rules drafts")
    layer = next((x for x in book.layers if x.file == file), None)
    if layer is None or layer.copy is None:
        return JobOutput(
            status="error", summary=f"policy draft: {file} names no copy to draft from"
        )
    try:
        _sha, pages = read_copy(layer, layer.copy)
        path = rules_file(tenant_dir(ctx.tenant_slug), file)
        text = path.read_text(encoding="utf-8")
        rule, new_text = draft(
            book,
            file,
            text,
            json.loads(str(ctx.params.get("rule") or "{}")),
            pages,
            roles=set(policy.roles),
            people=set(policy.people),
            kinds=list(ctx.tenant.expenses.category_accounts) or None,
        )
    except (CopyUnreadable, PolicyError, ValueError) as exc:
        return JobOutput(status="error", summary=f"policy draft: {exc}")
    if new_text == text:
        return JobOutput(status="ok", summary=f"policy draft: {rule.id} was already drafted")
    target = ctx.guard.check_write(path)
    if ctx.shadow:
        return JobOutput(status="ok", summary=f"policy draft (shadow): would add {rule.id}")
    ctx.record_now(
        f"draft:{file}:{rule.sha[:16]}",
        DRAFT_RECORD,
        {"file": file, "rule": rule.id, "sha": rule.sha, "by": person, "replaces": rule.replaces},
    )
    target.write_text(new_text, encoding="utf-8")
    return JobOutput(status="ok", summary=f"policy draft: {rule.id} added to {file}")


# ---- pay: a board's resolution setting a missionary's pay (c34) -----------------

PAY_RECORD = "policy.pay_drafted"
_PAY_PARAMS = ("person", "for", "yearly", "day", "from")


def _pay_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "policy-pay")
    for name in _PAY_PARAMS:
        key.param(name)
    book, error = _book(ctx)
    key.value("rules_error", error)
    key.value("rules", sorted(r.sha for r in book.rules) if book else [])
    key.config("identity.legal_name")
    return key.digest()


def _pay_said(ctx: JobContext, policy: Policy) -> tuple[str, str, Decimal, int, str]:
    """(person id, their name, yearly, day, first month) as asked, or a
    PolicyError saying what is wrong in plain words."""
    said = str(ctx.params.get("for", "")).strip()
    paid = policy.person_named(said) if said else None
    if paid is None:
        raise PolicyError(f"{said or 'nobody'} is not a person the ministry named")
    raw = str(ctx.params.get("yearly", "")).replace("$", "").replace(",", "").strip()
    try:
        yearly = Decimal(raw)
    except InvalidOperation as exc:
        raise PolicyError(f"the yearly pay is an amount, not {raw!r}") from exc
    if not yearly.is_finite() or yearly <= 0:
        raise PolicyError("the yearly pay is an amount more than $0")
    try:
        day = int(str(ctx.params.get("day", "")).strip())
    except ValueError:
        day = 0
    if not 1 <= day <= 28:
        raise PolicyError("the pay day is a day of the month, 1 to 28")
    start = str(ctx.params.get("from", "")).strip()
    if not MONTH.match(start):
        raise PolicyError(f"pay starts in a month, like 2027-01, not {start!r}")
    return paid, policy.display(paid), yearly, day, start


def _pay_run(ctx: JobContext) -> JobOutput:
    """Tim drafts the board's pay resolution for one person: its words and
    its one plan rule, into the ministry's rules folder. Nothing goes in
    force here; ``propose`` raises its card. Only someone who decides the
    ministry's rules sets pay."""
    from ...authority.routing import can_decide
    from ...engine.config import tenant_dir
    from ...policy import RULES_DIR, parse_layer
    from ...policy.pay import write

    book, error = _book(ctx)
    policy = ctx.authority.policy if ctx.authority is not None else None
    if error or policy is None:
        return JobOutput(status="error", summary=f"policy pay: {error or 'no authority here'}")
    asker = str(ctx.params.get("person", ""))
    if not can_decide(policy, CARD_AUTHORITY[RULE_CARD], asker):
        return JobOutput(status="error", summary="policy pay: only who decides the rules sets pay")
    adopters = sorted({x.adopted_by for x in (book.layers if book else ()) if x.plan} or {""})
    adopted_by = "board" if "board" in policy.roles else adopters[-1]
    try:
        paid, name, yearly, day, start = _pay_said(ctx, policy)
        if not adopted_by or paid in policy.holders(adopted_by):
            raise PolicyError(
                f"the {adopted_by or 'board'} adopts pay, and {name} is on it; "
                "pay needs a board the person it pays is not on"
            )
        written = write(
            ctx.tenant.identity.legal_name,
            person=paid,
            name=name,
            yearly=yearly,
            day=day,
            start=start,
            adopted_by=adopted_by,
        )
        parse_layer(
            tomllib.loads(written.rules),
            written.file,
            roles=set(policy.roles),
            people=set(policy.people),
        )
    except PolicyError as exc:
        return JobOutput(status="error", summary=f"policy pay: {exc}")
    folder = tenant_dir(ctx.tenant_slug) / RULES_DIR
    rules, words = folder / written.file, folder / written.copy
    if (
        rules.is_file()
        and rules.read_text(encoding="utf-8") == written.rules
        and words.is_file()
        and words.read_text(encoding="utf-8") == written.words
    ):
        return JobOutput(status="ok", summary=f"policy pay: {written.file} was already drafted")
    targets = [ctx.guard.check_write(rules), ctx.guard.check_write(words)]
    if ctx.shadow:
        return JobOutput(status="ok", summary=f"policy pay (shadow): would write {written.file}")
    ctx.record_now(
        f"pay:{written.file}:{hashlib.sha256(written.rules.encode()).hexdigest()[:16]}",
        PAY_RECORD,
        {"file": written.file, "for": paid, "yearly": f"{yearly:.2f}", "by": asker},
    )
    folder.mkdir(exist_ok=True)
    targets[1].write_text(written.words, encoding="utf-8")
    targets[0].write_text(written.rules, encoding="utf-8")
    return JobOutput(status="ok", summary=f"policy pay: {written.file} drafted for {name}")


JOBS: dict[str, JobHandler] = {
    "pay": JobHandler(key=_pay_key, run=_pay_run),
    "propose": JobHandler(key=_key, run=_propose_run),
    "ask": JobHandler(key=_ask_key, run=_ask_run),
    "watch": JobHandler(key=_watch_key, run=_watch_run),
    "draft": JobHandler(key=_draft_key, run=_draft_run),
}


# Deciding a rule is confirming an authority change: the floor keeps agents
# out, and a card with no money takes the person's own Face ID at the door.
# An OK before buying is approving that purchase, by the role the rule names
# when it names one; the amount counts against the ministry's dollar line.
# A new copy of a handbook goes to the same people as its rules.
# A plan is adopted by the role it names, never by anyone it covers (the
# floor refuses them by ``covers``), whatever the ministry's grants say.
CARD_AUTHORITY: dict[str, CardRule] = {
    RULE_CARD: CardRule("confirm", "authority", money=False),
    PLAN_CARD: CardRule(
        "confirm", "authority", money=False, role="adopted_by", covers="covers", kin="kin"
    ),
    COPY_CARD: CardRule("confirm", "authority", money=False),
    ASK_CARD: CardRule(
        "approve", "purchase.order", amount="amount", submitter="person", role="approver_role"
    ),
}
