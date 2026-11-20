"""The missionary answers the setup questions herself (engine docs/screens/
README.md, screens 4 to 9; board c49, the owner's go of 2026-11-17).

A maintainer makes a one-time link on the box (``engine interview new``).
The person answers six screens on her phone (core/tools/interview_page.py
draws them): how support reaches her, her IRS letter (the photo kept, the
name typed, unless the box reads it: see below), where she lives, who looks
over her spending and whether they also approve each month (c48), whether
she drives, and what Tim heard. Where the box's own file names
``letter_read`` (card R2), the letter's photo is read and its legal name
and Employer Identification Number said back for her tap
(core/tools/letter_read.py); her yes posts them as her answer. Her final
yes writes the same ministry file a maintainer drafts by hand today
(proving-ground ``practice-box/ministry.example.toml``), which the installer
builds a box from. Every answer goes through the setup questions' own
rules, and every assumption Tim says back is read from their defaults,
never typed here.

Support that comes only straight to her, with no nonprofit of her own, is a
setup shape Tim can't do yet (its own chunk): the interview says so, keeps
what she said, and leaves it for the maintainer. So does a church or an
organization.

Only a hash of the link's token is kept, as the box's other links do.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tomllib
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from . import _question, name_and_email
from .zones import ZoneAnswerError, resolve_zone

WORDS_FILE = Path(__file__).with_name("interview.toml")
SHAPE = "nonprofit-solo"
"""The one shape the interview sets up: a missionary with her own nonprofit."""

ORDER = ("support", "letter", "where", "reviewer", "driving")
"""The screens she answers, in order; then ``heard``, then ``done``."""

SUPPORT = ("own_nonprofit", "straight", "platform", "agency")
PRESENCE = ("expense.report", "payment", "authority")
"""As the hand-drafted file says: her reports and bills, and the accountable
plan her reviewer adopts, open to a yes through the door."""

INTERVIEWS = "_interviews"
"""The folder under the box's ledger root that holds its interviews (no
tenant's name starts with an underscore)."""

MAX_LETTER = 12 * 1024 * 1024
_PHOTO = (b"\xff\xd8\xff", b"\x89PNG", b"%PDF")
_HEIC = (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc")
_EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
_SUFFIX = re.compile(r"[,\s]+(inc|incorporated|llc|ltd)\.?$", re.I)
_NOT_ISSUED = frozenset("00 07 08 09 17 18 19 28 29 49 69 70 78 79 89 96 97".split())
"""The first two digits the IRS never issues an Employer Identification
Number under (its campus prefixes skip these)."""


def ein_of(said: str) -> str:
    """An Employer Identification Number as "12-3456789", or "" for anything
    that can't be one: nine digits (a dash or spaces between them are fine),
    under a prefix the IRS issues."""
    digits = re.sub(r"[\s-]", "", said)
    if not re.fullmatch(r"\d{9}", digits) or digits[:2] in _NOT_ISSUED:
        return ""
    return f"{digits[:2]}-{digits[2:]}"


@cache
def words() -> dict:
    with WORDS_FILE.open("rb") as handle:
        return tomllib.load(handle)


def all_words() -> list[str]:
    """Every string a person can read, for the words test."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(words())
    return found


def _first(name: str) -> str:
    return name.split()[0] if name.split() else name


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:31].strip("-")


def ministry_name(legal: str) -> str:
    """ "Hope Springs Ministries Inc." is called Hope Springs Ministries."""
    return _SUFFIX.sub("", legal.strip()).strip()


def _label(qid: str, value: str) -> str:
    """The words a setup question's own choice says, for its value."""
    for choice in _question(qid).choices:
        if str(choice["value"]) == value:
            return str(choice["label"])
    raise KeyError(f"{qid}: no choice {value!r}")


def _default(qid: str) -> str:
    said = _question(qid).default_for(SHAPE)
    if said is None:
        raise KeyError(f"{qid} has no default for {SHAPE}")
    return str(said)


def _dollars(amount: str) -> str:
    value = Decimal(amount)
    return f"${value:,.0f}" if value == value.to_integral() else f"${value:,.2f}"


def _month(n: int) -> str:
    return (
        "January February March April May June July August September October November December"
    ).split()[(n - 1) % 12]


def _zone(town: str) -> tuple[str, str]:
    """(zone, "") for where she lives, or ("", what to ask). The country is
    tried first ("Nakuru, Kenya" is Kenya's one zone), then the whole
    answer, then the town."""
    parts = [p.strip() for p in town.split(",") if p.strip()]
    tries = [parts[-1], town.strip(), parts[0]] if parts else [town]
    asked = ""
    for said in dict.fromkeys(tries):
        try:
            return resolve_zone(said), ""
        except ZoneAnswerError as exc:
            asked = asked or str(exc)
    return "", asked


def so_ill(answers: dict) -> list[str]:
    """What Tim does with where she lives, said back on the same screen."""
    w = words()["where"]
    zone = str(answers.get("zone", ""))
    if not zone:
        return []
    city = zone.rsplit("/", 1)[-1].replace("_", " ")
    return [w["days"].replace("{city}", city), w["money"], w["abroad"]]


def heard(answers: dict, *, name: str) -> dict[str, Any]:
    """Screen 9: what she said, and what Tim assumes, each assumption read
    from the setup questions' defaults for her shape."""
    w = words()["heard"]
    reviewer = answers.get("reviewer") or {}
    who = str(reviewer.get("name", ""))
    if who and answers.get("approves") == "approve":
        looks = w["reviewer_approves"].replace("{who}", who)
    else:
        looks = who or w["nobody"]
    legal = str(answers.get("legal_name", ""))
    if answers.get("ein"):  # read off her letter and tapped right (card R2)
        legal = w["ministry_ein"].replace("{name}", legal).replace("{ein}", str(answers["ein"]))
    mine = [
        (w["ministry"], legal),
        (
            w["you"],
            w["you_in"].replace("{name}", name).replace("{town}", str(answers.get("town", ""))),
        ),
        (w["reviewer"], looks),
        (w["driving"], w["drives"] if answers.get("drives") == "yes" else w["no_drives"]),
    ]
    start = int(_default("fiscal_start"))
    cash = _dollars(_default("cash_on_hand"))
    assumed = [
        w["year"].replace("{start}", _month(start)).replace("{end}", _month(start - 1)),
        w["mileage"],
        w["line"].replace("{line}", _dollars(_default("yes_by_word_up_to"))),
        w["cash"].replace("{cash}", cash).replace("{who}", _first(who))
        if who
        else w["cash_nobody"].replace("{cash}", cash),
    ]
    return {"mine": mine, "assumed": assumed}


class InterviewBook:
    """The box's interviews: one JSON file per link, named by its token's
    hash, and the letter's photo beside it. Written atomically."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)

    # ---- the link

    def mint(self, *, name: str, email: str, by: str, days: int = 14, now: float) -> str:
        """A fresh link token for ``name``; only its hash is kept."""
        if not name.strip() or not _EMAIL.match(email.strip()):
            raise ValueError("a name and an email address, please")
        token = secrets.token_urlsafe(24)
        self._save(
            self._key(token),
            {
                "name": " ".join(name.split()),
                "email": email.strip(),
                "by": " ".join(by.split()),
                "made": int(now),
                "expires": int(now + days * 86_400),
                "status": "open",
                "answers": {},
            },
        )
        return token

    def _key(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()[:32]

    def _path(self, key: str) -> Path:
        return self.folder / f"{key}.json"

    def _load(self, token: str, now: float | None = None) -> dict | None:
        if not token or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
            return None
        path = self._path(self._key(token))
        if not path.is_file():
            return None
        row = json.loads(path.read_text(encoding="utf-8"))
        if now is not None and row["status"] == "open" and now > row["expires"]:
            return None
        return row

    def _save(self, key: str, row: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self._path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self._path(key))

    # ---- reading it

    def person(self, token: str) -> dict:
        row = self._load(token) or {}
        return {k: row.get(k, "") for k in ("name", "email", "by")}

    def status(self, token: str) -> str:
        row = self._load(token)
        return row["status"] if row else ""

    def answers(self, token: str) -> dict:
        row = self._load(token)
        return dict(row["answers"]) if row else {}

    def keep_letter(self, token: str, photo: bytes) -> str:
        """Her letter's photo, kept beside her answers: "" when kept, else
        what to ask."""
        w = words()["letter"]
        if len(photo) > MAX_LETTER:
            return w["too_big"]
        if not (photo.startswith(_PHOTO) or photo[4:12] in _HEIC):
            return w["not_a_photo"]
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / f"{self._key(token)}.letter").write_bytes(photo)
        return ""

    def letter(self, token: str) -> bytes | None:
        path = self.folder / f"{self._key(token)}.letter"
        return path.read_bytes() if path.is_file() else None

    def next_screen(self, token: str, *, now: float) -> str | None:
        """The screen she sees now; None for a link that isn't one, or ran out."""
        row = self._load(token, now)
        if row is None:
            return None
        if row["status"] in ("done", "not_yet"):
            return row["status"]
        answers = row["answers"]
        if answers.get("changing"):
            return str(answers["changing"])
        return next((s for s in ORDER if s not in answers.get("answered", [])), "heard")

    def rows(self) -> list[dict]:
        """Every interview on the box, for the maintainer's list."""
        out = []
        for path in sorted(self.folder.glob("*.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            answers = row["answers"]
            out.append(
                {
                    "key": path.stem,
                    "name": row["name"],
                    "email": row["email"],
                    "by": row["by"],
                    "status": row["status"],
                    "expires": row["expires"],
                    "support": answers.get("support", []),
                    "legal_name": answers.get("legal_name", ""),
                    "ein": answers.get("ein", ""),
                    "church": bool(answers.get("church")),
                    "finished": row.get("finished", 0),
                    "file": str(self.folder / f"{path.stem}.ministry.toml")
                    if row["status"] == "done"
                    else "",
                }
            )
        return out

    # ---- taking an answer

    def take(
        self, token: str, screen: str, form: dict, *, photo: bytes | None = None, now: float
    ) -> str:
        """Her answer to ``screen``: "" when it is kept, else what to ask."""
        row = self._load(token, now)
        if row is None:
            return words()["timed_out"]
        if row["status"] != "open" or screen != self.next_screen(token, now=now):
            return words()["timed_out"]
        answers = row["answers"]
        said = _ANSWER[screen](answers, form, photo, self, token)
        if said:
            return said
        if screen == "heard":
            if form.get("change") in ORDER:
                answers["changing"] = form["change"]
            elif form.get("right") == "yes":
                row["status"] = "done"
                row["finished"] = int(now)
        else:
            answers.pop("changing", None)
            answers["answered"] = list(dict.fromkeys([*answers.get("answered", []), screen]))
            if screen == "support" and (
                "own_nonprofit" not in answers["support"] or answers.get("church")
            ):
                row["status"] = "not_yet"
                row["finished"] = int(now)
        self._save(self._key(token), row)
        if row["status"] == "done":
            path = self.folder / f"{self._key(token)}.ministry.toml"
            path.write_text(self.ministry_file(token), encoding="utf-8")
        return ""

    # ---- the ministry file

    def ministry_file(self, token: str) -> str:
        """The file the installer takes, as a maintainer drafts it by hand
        (proving-ground practice-box/ministry.example.toml); "" until her
        yes on what Tim heard."""
        row = self._load(token)
        if row is None or row["status"] != "done":
            return ""
        a = row["answers"]
        legal = str(a["legal_name"])
        slug = _slug(ministry_name(legal))
        said: list[tuple[str, str]] = [
            ("who", _label("who", SHAPE)),
            ("legal_name", legal),
            ("your_name", f"{row['name']}, {row['email']}"),
            ("timezone", str(a["zone"])),
            ("fiscal_start", _default("fiscal_start")),
            ("entity_nonprofit", _label("entity_nonprofit", _default("entity_nonprofit"))),
        ]
        reviewer = a.get("reviewer") or {}
        if reviewer:
            said.append(("reviewer", f"{reviewer['name']}, {reviewer['email']}"))
            said.append(
                ("reviewer_approves", _label("reviewer_approves", a.get("approves", "look")))
            )
        said.append(("cash_on_hand", _default("cash_on_hand")))
        said.append(("spelling", _label("spelling", _default("spelling"))))
        head = [
            f"name = {json.dumps(slug)}",
            f"slug = {json.dumps(slug)}",
            f"ministry = {json.dumps(ministry_name(legal))}",
            f"presence = {json.dumps(list(PRESENCE)).replace(',', ', ').replace(',  ', ', ')}",
            "",
        ]
        body = [f"[[setup]]\nid = {json.dumps(q)}\nsaid = {json.dumps(v)}" for q, v in said]
        return "\n".join(head + body) + "\n"


# ---- each screen's answer: "" when kept, else what to ask --------------------


def _picked(form: dict, key: str) -> list[str]:
    value = form.get(key, [])
    return [value] if isinstance(value, str) else list(value)


def _support(answers: dict, form: dict, photo: bytes | None, book: Any, token: str) -> str:
    picks = [p for p in SUPPORT if p in _picked(form, "support")]
    church = form.get("church") == "yes"
    if not picks and not church:
        return words()["support"]["none"]
    answers["support"] = picks
    if church:
        answers["church"] = True
    return ""


def _letter(answers: dict, form: dict, photo: bytes | None, book: Any, token: str) -> str:
    w = words()["letter"]
    legal = " ".join(str(form.get("legal_name", "")).split())
    if not legal:
        return w["no_name"]
    if photo:
        kept = book.keep_letter(token, photo)
        if kept:
            return kept
    answers["legal_name"] = legal
    # The number comes only with her yes to what the letter reads (card R2):
    # the form she taps carries it. A number that can't be one is left out.
    ein = ein_of(str(form.get("ein", "")))
    if ein:
        answers["ein"] = ein
    else:
        answers.pop("ein", None)
    return ""


def _where(answers: dict, form: dict, photo: bytes | None, book: Any, token: str) -> str:
    town = " ".join(str(form.get("town", "")).split())
    zone, ask = _zone(town)
    if not zone:
        return ask
    answers["town"], answers["zone"] = town, zone
    return ""


def _reviewer(answers: dict, form: dict, photo: bytes | None, book: Any, token: str) -> str:
    w = words()["reviewer"]
    if form.get("nobody") == "yes":
        answers["reviewer"] = None
        answers.pop("approves", None)
        return ""
    name = " ".join(str(form.get("reviewer_name", "")).split())
    email = str(form.get("reviewer_email", "")).strip()
    if not name:
        return w["no_name"]
    if not _EMAIL.match(email):
        return w["no_email"]
    name, email = name_and_email("reviewer", f"{name}, {email}")
    answers["reviewer"] = {"name": name, "email": email}
    answers["approves"] = "approve" if form.get("approves") == "approve" else "look"
    return ""


def _driving(answers: dict, form: dict, photo: bytes | None, book: Any, token: str) -> str:
    answers["drives"] = "yes" if form.get("drives") == "yes" else "no"
    return ""


def _heard(answers: dict, form: dict, photo: bytes | None, book: Any, token: str) -> str:
    if form.get("right") == "yes" or form.get("change") in ORDER:
        return ""
    return words()["heard"]["pick"]


_ANSWER = {
    "support": _support,
    "letter": _letter,
    "where": _where,
    "reviewer": _reviewer,
    "driving": _driving,
    "heard": _heard,
}


def notices(book: InterviewBook, since: float) -> list[dict]:
    """A note to whoever sent each link once she finished or stopped, for
    ``doorkeeper send-notices`` (kind ``interview``; sent once by its id).
    ``to`` is the sender as the box's people list names them, the slug of
    their name, as setup writes it; a sender the box has no address for is
    skipped there."""
    out = []
    for row in book.rows():
        if row["status"] not in ("done", "not_yet") or row["finished"] < since:
            continue
        stands = "church" if row["church"] else row["status"]
        out.append(
            {
                "id": f"interview-{row['key'][:16]}-{stands}",
                "to": _slug(row["by"]),
                "kind": "interview",
                "about": row["name"],
                "stands": stands,
            }
        )
    return out


__all__ = [
    "INTERVIEWS",
    "InterviewBook",
    "all_words",
    "ein_of",
    "heard",
    "ministry_name",
    "notices",
    "so_ill",
    "words",
]
