"""AP vendor registry: schema + resolution for a tenant's ``vendors.toml``.

The registry file lives under ``tenants/<slug>/vendors.toml`` (tenant data,
never in core). Shape, ported from the legacy system and recorded in the
fidelity contract: **top-level keys are sender domains or stable name
fragments**, each mapping to a vendor entry.

Precedence (locked at the Phase 2 checkpoint): this registry answers "which
canonical vendor does this sender/subject belong to" plus that vendor's coding
defaults. Identity resolution order in intake: sender domain, then subject
alias, then extracted-name match.

One-EIN-two-rows: distinct billing names that share a taxpayer carry the same
``tax_entity`` value. They stay separate vendors (separate rows, separate
invoices) but verification and 1099 logic can treat them as one tax entity via
``tax_siblings``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

# A trailing "(alias)" parenthetical on a vendor name: "Vendor (DBA X)" from a
# PDF header vs "Vendor" keyed by hand must not split identity (2026-06-16
# shadow finding).
_ALIAS_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


class VendorEntry(BaseModel):
    vendor: str  # canonical display name
    gl_account: str = ""
    cost_type: str = ""
    subject_aliases: list[str] = Field(default_factory=list)
    # Exact sender addresses that speak for this vendor when its mail does not
    # come from a domain of its own (a free-mail address, a bookkeeper's
    # mailbox). Sender provenance (#356) binds these exactly; a free-mail
    # domain never binds a vendor by itself.
    senders: list[str] = Field(default_factory=list)
    # Alternate spellings other systems use for this same vendor, e.g. the
    # legacy ledger may write a fuller or abbreviated name than the engine
    # files. Matched exactly (never guessed) so identity cannot drift
    # (2026-06-23 shadow finding). Distinct from subject_aliases, which are
    # substring fragments for matching email subjects on intake.
    ledger_aliases: list[str] = Field(default_factory=list)
    payment_channel: str = ""  # tenant vocabulary; engine treats as opaque
    last_confirmed_channel: str = ""  # ISO date or empty
    channel_note: str = ""
    tax_entity: str = ""  # same value across entries = same taxpayer EIN
    # Federal tax classification from the vendor's W-9 checkbox (e.g. "s-corp",
    # "c-corp", "individual/sole-prop", "partnership"). Corporations are exempt
    # from 1099-NEC reporting. Empty means unknown/unstated: 1099 logic must
    # treat empty as "assume reportable", never as exempt.
    tax_classification: str = ""
    # The 1099-NEC line-1 recipient when it differs from the display name: a
    # disregarded single-member LLC reports under its OWNER as written on the
    # W-9 line 1 (2026-07-28 lesson; pairing the owner name with the entity
    # EIN is an IRS TIN-match failure). Empty means the display name IS the
    # recipient. Names only — TINs never enter this repo.
    tax_recipient: str = ""


class VendorRegistry(BaseModel):
    entries: dict[str, VendorEntry] = Field(default_factory=dict)

    def resolve_sender(self, sender: str) -> VendorEntry | None:
        """Match an email address or domain against the registry keys.

        A key that looks like a domain matches the sender's domain exactly or
        as a suffix; a bare name-fragment key (legacy convention) matches
        anywhere inside the domain.
        """
        domain = sender.rsplit("@", 1)[-1].strip().lower()
        for key, entry in self.entries.items():
            k = key.lower()
            if domain == k or domain.endswith("." + k) or k in domain:
                return entry
        return None

    def resolve_subject(self, subject: str) -> VendorEntry | None:
        """Match subject aliases (case-insensitive substring), longest first."""
        low = subject.lower()
        best: tuple[int, VendorEntry] | None = None
        for entry in self.entries.values():
            for alias in entry.subject_aliases:
                a = alias.lower()
                if a and a in low and (best is None or len(a) > best[0]):
                    best = (len(a), entry)
        return best[1] if best else None

    def resolve_name(self, name: str) -> VendorEntry | None:
        """Match a canonical vendor name, case-insensitively."""
        low = name.strip().lower()
        for entry in self.entries.values():
            if entry.vendor.lower() == low:
                return entry
        return None

    def resolve_ledger_name(self, name: str) -> VendorEntry | None:
        """Match a canonical vendor name OR any listed ledger alias, exactly.

        Used to reconcile the engine's vendor spelling against the legacy
        ledger's. Exact and case-insensitive only: a name resolves only if it
        is the canonical name or a spelling written down in ``ledger_aliases``,
        so two genuinely different vendors are never merged.
        """
        low = name.strip().lower()
        for entry in self.entries.values():
            if entry.vendor.lower() == low:
                return entry
            if any(alias.strip().lower() == low for alias in entry.ledger_aliases):
                return entry
        return None

    def tax_siblings(self, vendor_name: str) -> list[VendorEntry]:
        """Every entry sharing the vendor's taxpayer, the vendor included."""
        me = self.resolve_name(vendor_name)
        if me is None:
            return []
        if not me.tax_entity:
            return [me]
        return [e for e in self.entries.values() if e.tax_entity == me.tax_entity]


def normalize_vendor_token(name: str) -> str:
    """Drop a trailing parenthetical alias, then normalize case and whitespace.

    "Vendor Name (ALIAS)" and "Vendor Name" collapse to one token. A name that
    is *entirely* a parenthetical keeps its original text, so distinct
    placeholders do not all become the empty string.
    """
    stripped = _ALIAS_SUFFIX.sub("", name.strip())
    return (stripped or name.strip()).lower()


def canonical_vendor(name: str, registry: VendorRegistry | None = None) -> str:
    """One canonical identity token for a vendor spelling.

    The single vendor-identity function shared by the shadow parity diff and the
    three-way payment verification, so the two matchers can never drift apart (a
    spelling that reconciles in the diff must also reconcile a committed payment,
    invariant 4). When a registry is given and the name resolves to a known
    vendor or one of its ``ledger_aliases``, return that vendor's normalized
    canonical token; otherwise normalize the name itself. Exact and
    case-insensitive only, so two genuinely distinct vendors never merge.
    """
    if registry is not None:
        entry = registry.resolve_ledger_name(name)
        if entry is not None:
            return normalize_vendor_token(entry.vendor)
    return normalize_vendor_token(name)


def load_vendor_registry(path: str | Path) -> VendorRegistry:
    from ...engine.runkey import touch

    # The registry is a run input wherever it is loaded (#153): the trail
    # records the load so a run key that omits it fails the audit.
    touch("registry:vendors")
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no vendor registry at {path}")
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    entries = {key: VendorEntry.model_validate(value) for key, value in raw.items()}
    return VendorRegistry(entries=entries)
