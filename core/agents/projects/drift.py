"""The project registry's drift report, read as items and turned into cards (#325).

Pure code: no I/O beyond what the caller passes in, no model. The report is
written by the tenant's own registry sync (outside the engine) and is already
a stable contract (the auditor's lens 17 reads it). Each bullet is one item,
classified into exactly one kind; a bullet matching none is ``unclassified``,
reported and never acted on. The project number is the bullet's own first
word, so no project-number format lives in core (#340).

A card is keyed on (project, kind), never on the report's filename, so next
week's report repeating an item never asks again once it is decided.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DRIFT_ACTION = "projects.registry_drift"

# kind -> words in the BULLET that mean it, first match wins
_TEXT_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("qbo-project-missing", ("no qbo project",)),
    ("folder-missing", ("no project-drive folder", "no file-store folder", "but no folder")),
    ("one-source", ("only one source",)),
    ("new-since-sync", ("new since last sync",)),
)
# kind -> words in the SECTION HEADING above a bullet, when its own text is terse
_HEAD_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("qbo-project-missing", ("missing from qbo",)),
    ("folder-missing", ("folders",)),
    ("one-source", ("low-confidence",)),
    ("new-since-sync", ("newly detected",)),
)
DECISION_ONLY = frozenset({"one-source", "new-since-sync"})


@dataclass(frozen=True)
class DriftItem:
    pn: str
    name: str
    kind: str
    text: str


@dataclass(frozen=True)
class DriftCard:
    key: str
    item: DriftItem
    action_type: str = DRIFT_ACTION


@dataclass
class CreateResult:
    verified: bool
    events: list[dict] = field(default_factory=list)
    anomaly: str = ""
    registry_writes: tuple = ()


def _classify(text: str, heading: str) -> str:
    for table, haystack in ((_TEXT_WORDS, text.lower()), (_HEAD_WORDS, heading.lower())):
        for kind, words in table:
            if any(w in haystack for w in words):
                return kind
    return "unclassified"


def parse_report(text: str) -> list[DriftItem]:
    items: list[DriftItem] = []
    heading = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            continue
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        if not body or body.lower() in ("(none)", "none"):
            continue
        head = body.split(":", 1)[0].strip()
        pn, _, name = head.partition(" ")
        items.append(DriftItem(pn=pn, name=name.strip(), kind=_classify(body, heading), text=body))
    return items


def propose_cards(items: list[DriftItem], *, answered: frozenset[str]) -> list[DriftCard]:
    """One card per actionable item not already carded or decided."""
    cards: list[DriftCard] = []
    seen: set[str] = set()
    for item in items:
        if item.kind == "unclassified":
            continue
        key = f"{item.pn}:{item.kind}"
        if key in answered or key in seen:
            continue
        seen.add(key)
        cards.append(DriftCard(key=key, item=item))
    return cards


def project_display_name(item: DriftItem) -> str:
    return f"{item.pn} {item.name}".strip()


def execute_project_create(client, item: DriftItem, *, parent: str) -> CreateResult:
    """Create the accounting system's Project and record it only when a
    readback shows it (the 2026-08-03 contract). Never writes the registry."""
    name = project_display_name(item)
    client.create_project(name, parent)
    found = client.find_project(name)
    if not found:
        return CreateResult(
            verified=False,
            anomaly=f"{item.pn}: the create was accepted but no project named {name!r} "
            "reads back; not recorded, not retried",
        )
    return CreateResult(
        verified=True,
        events=[{"pn": item.pn, "name": name, "qbo_id": str(found.get("Id", ""))}],
    )
