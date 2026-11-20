"""Gifts through a payment service (board chunk c32, the PayPal half; the
owner, 2026-11-12: PayPal first, built for any service a ministry uses).

Ministries take gifts through PayPal, Stripe, Zeffy, Givebutter, Donorbox,
Square, Venmo, and church giving apps (Tithe.ly, Pushpay, Subsplash,
Planning Center Giving). Each exports a CSV with the same few facts under
its own headers: the day, who gave, the gross, the service's fee, the net,
the row's type and status, and a transaction id. One reader finds those
columns by name, the way ``statements.parse`` finds Debit and Credit, so
every service goes through the same code.

The service is the ministry's own money account in the books
(``Assets:Processor:<service>:<person>``, its own balance), never lines on
the bank's account: mixing the two would break the bank's tie-out at every
month end. So:

- A completed gift is the ministry's income at its gross, its donor named
  from the export; the service's fee is spending (the ministry's bank-fee
  kind); the net is what the service holds.
- A payout to the bank is money moving between the ministry's own
  accounts, never income: the bank deposit it became (the same dollars
  within a week after) is matched by code on every read, and that deposit
  carries the move, so the bank keeps tying to the bank's own balance. Its
  gross counts once, at the service, for the 990.
- A refund is the gift reversed: income less its gross, the fee the
  service gave back off the fee.
- Rows that are neither (a pending payment, a hold, a currency
  conversion, a purchase paid from the balance) are left out and counted,
  so the person hears what Tim didn't keep.

Pure functions, no I/O, no clock. The door parses and holds; the expenses
``processor`` job keeps; the month reads the record.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from . import statements as st

PAYMENT_EVENT = "processor.payment"  # one kept row of a payment service's export

GIFT, REFUND, PAYOUT = "gift", "refund", "payout"
ROOT = "Assets:Processor"
"""Where the books keep each service's balance: ``<ROOT>:<service>:<person>``."""

PAYOUT_REACH = 7
"""How many days after a payout its bank deposit may land (a service's
standard transfer is a business day or a few)."""

GIFT_KINDS = ("Gifts", "Donations", "Contributions")
"""Where a gift reads when the export names no fund the ministry keeps."""

_DAY_HEADS = (
    "date",
    "received date",
    "date received",
    "received at",
    "donation date",
    "payment date",
    "transaction date",
    "created",
    "created (utc)",
)
_GROSS_HEADS = ("gross", "gross amount", "amount", "donation amount", "total amount", "total")
_FEE_HEADS = ("fee", "fees", "fee amount", "processing fee", "processing fees", "total fees")
_NET_HEADS = ("net", "net amount", "amount net", "net total")
_NAME_HEADS = ("name", "donor name", "donor", "full name", "person name", "customer name")
_FIRST_HEADS = ("first name", "donor first name")
_LAST_HEADS = ("last name", "donor last name")
_TYPE_HEADS = ("type", "transaction type", "payment type")
_STATUS_HEADS = ("status", "transaction status", "payment status")
_ID_HEADS = ("transaction id", "donation id", "payment id", "receipt id", "id")
_CURRENCY_HEADS = ("currency",)
_FUND_HEADS = ("fund", "fund name", "designation", "campaign")

DONE = frozenset({"", "completed", "complete", "succeeded", "success", "paid", "posted", "settled"})
"""The statuses of money that moved; anything else (pending, denied, failed)
is left out."""

_MOVES = re.compile(r"withdraw|payout|transfer|to bank|bank deposit|sweep", re.I)
_REFUNDS = re.compile(r"refund|reversal|chargeback", re.I)
_SKIPS = re.compile(r"\bhold\b|currency conversion|authori[sz]ation", re.I)
_FUND_GRANTS = re.compile(
    r"giving fund|charitable|donor.advised|christian foundation|\bdaf\b|benevity", re.I
)
"""A donor-advised fund or giving platform grant: a thank-you only (donors.py)."""

SERVICES = (("PayPal", ("from email address",)),)
"""A service told by a header only its export carries; any other asks."""


class NotAnExport(ValueError):
    """Not a payment service's export, or one this code won't guess at."""

    def __init__(self, why: str, say: str = "") -> None:
        super().__init__(why)
        self.say = say


@dataclass(frozen=True)
class Payment:
    """One row of a service's export that moved money: a gift, a refund, or
    a payout. Cents are signed as the service says them: a gift's gross and
    net positive, a refund's and a payout's negative."""

    day: str
    kind: str
    gross: int
    net: int
    name: str = ""
    words: str = ""
    txn: str = ""
    fund: str = ""
    n: int = 0

    @property
    def fee(self) -> int:
        """What the service kept (positive), or gave back on a refund (negative)."""
        return self.gross - self.net

    def sha(self, person: str, service: str) -> str:
        """The row's fingerprint: its transaction id when the export gives
        one, else its day, gross and place among its twins."""
        ident = self.txn or f"{self.day}|{self.gross}|{self.n}"
        return hashlib.sha256(f"processor:{person}|{service}|{ident}".encode()).hexdigest()

    def payload(self, person: str, service: str) -> dict:
        return {
            "sha256": self.sha(person, service),
            "person": person,
            "service": service,
            "day": self.day,
            "kind": self.kind,
            "gross": self.gross,
            "net": self.net,
            "name": self.name,
            "words": self.words,
            "txn": self.txn,
            "fund": self.fund,
            "n": self.n,
        }

    @classmethod
    def of(cls, p: dict) -> Payment:
        return cls(
            str(p["day"]),
            str(p["kind"]),
            int(p["gross"]),
            int(p["net"]),
            str(p.get("name", "")),
            str(p.get("words", "")),
            str(p.get("txn", "")),
            str(p.get("fund", "")),
            int(p.get("n", 0)),
        )


@dataclass(frozen=True)
class Export:
    """A service's export as read: who it is ("" when Tim must ask), the
    rows that moved money, and the types of the rows left out."""

    service: str
    payments: tuple[Payment, ...]
    left_out: tuple[str, ...]

    @property
    def gifts(self) -> int:
        return sum(1 for p in self.payments if p.kind == GIFT)


def _find(head: list[str], names: tuple[str, ...]) -> int | None:
    return st._find(head, names)


def _header(head: list[str]) -> bool:
    """A service's header: a day, money in, and the fee or the net. A bank
    statement carries no fee or net column, so never reads as one."""
    if _find(head, _DAY_HEADS) is None:
        return False
    money = _find(head, _GROSS_HEADS) is not None or _find(head, _NET_HEADS) is not None
    return money and (_find(head, _FEE_HEADS) is not None or _find(head, _NET_HEADS) is not None)


def is_export(text: str) -> bool:
    """Whether the file reads as a payment service's export at all."""
    try:
        rows = st._rows(text)
    except st.NotAStatement:
        return False
    return any(_header(head) for head in rows[: st._SCAN_ROWS])


def _service(head: list[str]) -> str:
    low = {h.casefold() for h in head}
    return next((name for name, marks in SERVICES if all(m in low for m in marks)), "")


def _amount(cell: str) -> int | None:
    try:
        return st._money(cell)
    except st.NotAStatement as exc:
        raise NotAnExport(str(exc), exc.say) from None


def parse(text: str) -> Export:
    """Every row of a service's export that moved money, oldest first."""
    rows = st._rows(text)
    at = next((i for i, head in enumerate(rows[: st._SCAN_ROWS]) if _header(head)), None)
    if at is None:
        raise NotAnExport("no Date, Gross, and Fee (or Net) header")
    head = rows[at]
    col = {
        key: _find(head, names)
        for key, names in (
            ("day", _DAY_HEADS),
            ("gross", _GROSS_HEADS),
            ("fee", _FEE_HEADS),
            ("net", _NET_HEADS),
            ("name", _NAME_HEADS),
            ("first", _FIRST_HEADS),
            ("last", _LAST_HEADS),
            ("type", _TYPE_HEADS),
            ("status", _STATUS_HEADS),
            ("id", _ID_HEADS),
            ("currency", _CURRENCY_HEADS),
            ("fund", _FUND_HEADS),
        )
    }
    seen: dict[tuple[str, int], int] = {}
    out: list[Payment] = []
    left: list[str] = []
    for row in rows[at + 1 :]:

        def cell(key: str, row: list[str] = row) -> str:
            i = col[key]
            return " ".join(row[i].split()) if i is not None and i < len(row) else ""

        day = st.day_of(cell("day").split(" ")[0]) if cell("day") else None
        if day is None:
            continue  # a total, a footer
        kind_words = cell("type")
        if cell("status").casefold() not in DONE or _SKIPS.search(kind_words):
            left.append(kind_words or cell("status") or "a row")
            continue
        if cell("currency") and cell("currency").upper() != "USD":
            left.append(f"{kind_words or 'a row'} in {cell('currency').upper()}")
            continue
        gross, fee, net = _amount(cell("gross")), _amount(cell("fee")), _amount(cell("net"))
        if gross is None and net is None:
            continue
        gross = gross if gross is not None else net + abs(fee or 0)
        if net is None:
            net = gross - abs(fee or 0) if gross > 0 else gross + abs(fee or 0)
        elif fee is not None and abs(fee) != abs(gross - net):
            raise NotAnExport(
                f"gross {gross} less fee {fee} is not net {net}",
                "In that file a row's gross less its fee isn't its net, so Tim can't tell "
                "what the service kept. Export it again from the service's activity page.",
            )
        if gross > 0 and _MOVES.search(kind_words):
            left.append(kind_words)  # money added from the bank: not a gift
            continue
        kind = (
            PAYOUT
            if gross < 0 and _MOVES.search(kind_words)
            else REFUND
            if gross < 0 and _REFUNDS.search(kind_words)
            else GIFT
            if gross > 0
            else ""
        )
        if not kind:
            left.append(kind_words or "money out")  # a purchase from the balance
            continue
        name = cell("name") or " ".join(x for x in (cell("first"), cell("last")) if x)
        n = seen.get((day, gross), 0)
        seen[(day, gross)] = n + 1
        out.append(Payment(day, kind, gross, net, name, kind_words, cell("id"), cell("fund"), n))
    if not out:
        raise NotAnExport(
            "a service's header, but no completed gifts or payouts",
            "That file has no completed gifts or payouts in it. Export the service's "
            "activity for the months you want, with completed payments.",
        )
    return Export(_service(head), tuple(sorted(out, key=lambda p: (p.day, p.n))), tuple(left))


def left_out_words(left: Iterable[str]) -> str:
    """What Tim didn't keep from an export, as the person hears it."""
    counts: dict[str, int] = {}
    for kind in left:
        counts[kind] = counts.get(kind, 0) + 1
    return ", ".join(f"{n} {k}" if n > 1 else k for k, n in sorted(counts.items()))


# ---- the books ------------------------------------------------------------------------


def kept(log: Iterable[dict]) -> dict[str, dict]:
    """Every kept row, by its fingerprint (the first keeping stands)."""
    out: dict[str, dict] = {}
    for e in log:
        if e.get("event_type") == PAYMENT_EVENT:
            p = e.get("payload", {})
            out.setdefault(str(p.get("sha256")), p)
    return out


def account(service: str, person: str) -> str:
    return f"{ROOT}:{service}:{person}"


def moves(log: list[dict]) -> tuple[dict[str, dict], set[str]]:
    """Each payout matched to the bank deposit it became: the deposit's
    fingerprint to its payout, and the payouts matched. A deposit on the
    same person's ministry account, the payout's dollars, landing that day
    or within a week after; the nearest first, each matched once. Read on
    every call, so a statement sent before or after the export both work."""
    payouts = sorted(
        (p for p in kept(log).values() if p["kind"] == PAYOUT), key=lambda p: (p["day"], p["n"])
    )
    if not payouts:
        return {}, set()
    lines: dict[str, dict] = {}
    moved_back: set[str] = set()
    for e in log:
        p = e.get("payload", {})
        if e.get("event_type") == st.LINE_EVENT:
            lines.setdefault(str(p.get("sha256")), p)
        elif e.get("event_type") == st.TRANSFER_EVENT:
            moved_back.add(str(p.get("sha256")))
    deposits = [
        (sha, p)
        for sha, p in lines.items()
        if p["account"] == st.MINISTRY and int(p["cents"]) > 0 and sha not in moved_back
    ]
    to_bank: dict[str, dict] = {}
    for pay in payouts:
        near = [
            (sha, p)
            for sha, p in deposits
            if sha not in to_bank
            and p["person"] == pay["person"]
            and int(p["cents"]) == -int(pay["net"])
            and 0
            <= (date.fromisoformat(p["day"]) - date.fromisoformat(pay["day"])).days
            <= PAYOUT_REACH
        ]
        if near:
            sha, _p = min(near, key=lambda sp: (sp[1]["day"], sp[1]["n"]))
            to_bank[sha] = pay
    return to_bank, {str(p["sha256"]) for p in to_bank.values()}


def gross_in(log: list[dict], start: str, end: str) -> int:
    """Gifts through the services from ``start`` through ``end``, less
    refunds, at their gross: what the ministry received, for a 990."""
    return sum(
        int(p["gross"])
        for p in kept(log).values()
        if p["kind"] in (GIFT, REFUND) and start <= str(p["day"]) <= end
    )


def _sub(sha: str, part: str) -> str:
    return hashlib.sha256(f"{sha}:{part}".encode()).hexdigest()


def _kind_of(fund: str, categories: list[str]) -> str:
    from .categories import canonical

    for name in (fund, *GIFT_KINDS):
        if name and (found := canonical(name, categories)):
            return found
    return ""


def _row(p: dict, sha: str, role: str, cents: int, words: str, said: str, **more) -> dict:
    return {
        "sha256": sha,
        "person": str(p["person"]),
        "account": "",
        "day": str(p["day"]),
        "words": words,
        "cents": cents,
        "role": role,
        "receipt": "",
        "receipt_file": "",
        "category": "",
        "said": said,
        "waiting_on": "",
        "gave": "",
        "reported": False,
        "paid_from": st.PROCESSOR,
        "processor": str(p["service"]),
        **more,
    }


def month_rows(log: list[dict], month: str, categories: list[str]) -> list[dict]:
    """The month's rows from every service, shaped as ``statements.month_lines``
    rows: each gift (money in, its donor named) and its fee (spending from
    the service), each refund, and each payout (a move, waiting on the bank
    deposit it became until a statement shows it)."""
    _to_bank, matched = moves(log)
    fee_kind = next((c for n in st.BANK_FEES if (c := _canon(n, categories))), "")
    out: list[dict] = []
    for sha, p in sorted(kept(log).items(), key=lambda kv: (kv[1]["day"], kv[1]["n"])):
        if str(p["day"])[:7] != month:
            continue
        pay, service = Payment.of(p), str(p["service"])
        who = pay.name or "someone not named"
        if pay.kind == PAYOUT:
            out.append(
                _row(
                    p,
                    sha,
                    st.MOVED,
                    -pay.net,
                    f"{service} to the bank",
                    f"moved from {service} to the ministry's bank",
                    waiting_on="" if sha in matched else WAITING_DEPOSIT,
                )
            )
            continue
        gift = {"gift_from": pay.name} if pay.name else {}
        if gift and _FUND_GRANTS.search(pay.name):
            gift["fund_grant"] = True
        words = f"{'Gift' if pay.kind == GIFT else 'Refund'} through {service}: {who}"
        row = _row(p, sha, st.CAME_IN, pay.gross, words, f"a gift through {service}", **gift)
        row["category"] = _kind_of(pay.fund, categories)
        out.append(row)
        if pay.fee:
            out.append(
                _row(
                    p,
                    _sub(sha, "fee"),
                    st.MINISTRY_PAID,
                    pay.fee,
                    f"{service}'s fee: {who}",
                    f"{service}'s fee",
                    category=fee_kind,
                )
            )
    return out


WAITING_DEPOSIT = "the bank deposit it became (the bank statement shows it)"


def _canon(name: str, categories: list[str]) -> str:
    from .categories import canonical

    return canonical(name, categories) or ""


__all__ = [
    "GIFT",
    "PAYMENT_EVENT",
    "PAYOUT",
    "REFUND",
    "ROOT",
    "Export",
    "NotAnExport",
    "Payment",
    "account",
    "gross_in",
    "is_export",
    "kept",
    "left_out_words",
    "month_rows",
    "moves",
    "parse",
]
