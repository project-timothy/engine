"""Owner triage: the auditor proposes, the owner decides, silence accepts.

``tenants/<slug>/triage.toml`` is the owner's standing decision record, read
with the auditor's own parser (file as data, same as tenant.toml). Four
knobs, all optional — a missing file means every proposal stands as made:

    [advisory]
    muted_topics = ["coding-drift"]          # topic keys, silenced entirely

    [advisory.acknowledged]                  # per-item: the owner has answered;
    "coding-drift/Owner Person" = "why"      # drop the subject from that topic's
                                             # facts so the voice stops re-asking

    [findings]
    muted = ["filing/stalled-at-top-level",  # lens/condition class
             "filing",                        # a whole lens
             "a1b2c3d4e5f60718"]             # one item, by fingerprint

    [findings.severity]                      # override a proposed severity
    "approvals/stale-pending" = "INFO"

Muting is applied BEFORE the checklist reconcile, so a newly muted open
item resolves on the next nightly (visible once in Resolved — the owner
sees the decision take effect) and never re-announces.

Dated entries (2026-09-04). A ``muted`` entry may be a table instead of a
bare string, and an acknowledgment's value may be a table instead of a
bare answer:

    muted = [
        "filing",                                                  # undated: permanent
        {key = "heartbeat/stale", snooze_until = "2026-09-10"},    # a DEFER
        {key = "a1b2c3d4e5f60718", since = "2026-09-04", why = "..."},  # dated mute
    ]
    [advisory.acknowledged]
    "coding-drift/Owner Person" = {answer = "why", since = "2026-07-24"}

- ``snooze_until``: the item is muted while the date is in the future; on
  that date it comes back and is announced as NEW with "(snooze expired)".
  The entry is then inert; delete it or re-date it.
- ``since``: a dated mute or acknowledgment keeps applying, but once it is
  older than the tenant's ``ack_max_age_days`` (default 90) the triage lens
  reports it once as ``acknowledgment-aged`` ("re-confirm or delete"): bump
  the date to keep it, delete the entry to resume surveillance.
- An entry with no date keeps the original permanent semantics. An
  unreadable date reads as no date (the safe side: silence stays silent).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import default_tenants_dir
from .findings import SEVERITIES, Finding

TRIAGE_FILENAME = "triage.toml"


@dataclass(frozen=True)
class AgedEntry:
    kind: str  # "mute" | "acknowledgment"
    key: str
    since: date
    age_days: int


@dataclass(frozen=True)
class Triage:
    muted_topics: frozenset[str] = frozenset()
    muted_findings: frozenset[str] = frozenset()  # lens, lens/condition, or fingerprint
    severity_overrides: dict = field(default_factory=dict)  # "lens/condition" -> severity
    acknowledged: dict = field(default_factory=dict)  # "topic/subject" -> owner's answer
    snoozes: dict = field(default_factory=dict)  # mute key -> date it lifts
    mute_dates: dict = field(default_factory=dict)  # mute key -> date it was made
    ack_dates: dict = field(default_factory=dict)  # "topic/subject" -> date it was made
    as_of: date = field(default_factory=date.today)

    def is_topic_muted(self, topic: str) -> bool:
        return topic in self.muted_topics

    @staticmethod
    def _keys(finding: Finding) -> set[str]:
        return {finding.lens, f"{finding.lens}/{finding.condition}", finding.fingerprint}

    def _active_snoozes(self) -> set[str]:
        return {key for key, until in self.snoozes.items() if until > self.as_of}

    def is_finding_muted(self, finding: Finding) -> bool:
        return bool(self._keys(finding) & (self.muted_findings | self._active_snoozes()))

    def apply_severity(self, finding: Finding) -> Finding:
        override = self.severity_overrides.get(f"{finding.lens}/{finding.condition}")
        if override and override != finding.severity:
            return Finding(
                lens=finding.lens,
                subject=finding.subject,
                condition=finding.condition,
                severity=override,
                detail=finding.detail,
            )
        return finding

    def filter(self, findings: list[Finding]) -> list[Finding]:
        return [self.apply_severity(f) for f in findings if not self.is_finding_muted(f)]

    def announce_reasons(self, findings: list[Finding]) -> dict[str, str]:
        """Fingerprint -> announce reason for findings whose snooze has
        expired: the store uses it in place of new/returned so the report
        can say "(snooze expired)". Findings still muted carry nothing."""
        expired = {key for key, until in self.snoozes.items() if until <= self.as_of}
        reasons: dict[str, str] = {}
        for finding in findings:
            if self._keys(finding) & expired and not self.is_finding_muted(finding):
                reasons[finding.fingerprint] = "snooze-expired"
        return reasons

    def aged_entries(self, *, max_age_days: int) -> list[AgedEntry]:
        """Dated mutes and acknowledgments older than the window, oldest
        first (acknowledgments, then mutes, then by key for a stable order)."""
        aged: list[AgedEntry] = []
        for kind, dates in (("acknowledgment", self.ack_dates), ("mute", self.mute_dates)):
            for key, since in dates.items():
                age = (self.as_of - since).days
                if age >= max_age_days:
                    aged.append(AgedEntry(kind=kind, key=str(key), since=since, age_days=age))
        return sorted(aged, key=lambda a: (a.kind, a.key))


def _parse_date(raw: object) -> date | None:
    if isinstance(raw, date):
        return raw  # tomllib already parsed a bare date
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def load_triage(
    slug: str, *, tenants_dir: str | Path | None = None, as_of: date | None = None
) -> Triage:
    root = Path(tenants_dir) if tenants_dir else default_tenants_dir()
    path = root / slug / TRIAGE_FILENAME
    as_of = as_of or date.today()
    if not path.exists():
        return Triage(as_of=as_of)
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    advisory = raw.get("advisory", {})
    findings = raw.get("findings", {})
    overrides = {}
    for key, severity in (findings.get("severity", {}) or {}).items():
        if severity in SEVERITIES:
            overrides[str(key)] = str(severity)

    muted: set[str] = set()
    snoozes: dict[str, date] = {}
    mute_dates: dict[str, date] = {}
    for entry in findings.get("muted", []) or []:
        if not isinstance(entry, dict):
            muted.add(str(entry))
            continue
        key = str(entry.get("key", "")).strip()
        if not key:
            continue
        until = _parse_date(entry["snooze_until"]) if "snooze_until" in entry else None
        if until is not None:
            snoozes[key] = until
            continue
        muted.add(key)
        since = _parse_date(entry["since"]) if "since" in entry else None
        if since is not None:
            mute_dates[key] = since

    acknowledged: dict[str, str] = {}
    ack_dates: dict[str, date] = {}
    for key, value in (advisory.get("acknowledged", {}) or {}).items():
        if isinstance(value, dict):
            acknowledged[str(key)] = str(value.get("answer", ""))
            since = _parse_date(value["since"]) if "since" in value else None
            if since is not None:
                ack_dates[str(key)] = since
        else:
            acknowledged[str(key)] = str(value)

    return Triage(
        muted_topics=frozenset(str(t) for t in advisory.get("muted_topics", [])),
        muted_findings=frozenset(muted),
        severity_overrides=overrides,
        acknowledged=acknowledged,
        snoozes=snoozes,
        mute_dates=mute_dates,
        ack_dates=ack_dates,
        as_of=as_of,
    )
