"""Run the audit: lenses -> reconcile -> report on disk.

A lens that crashes becomes a CRITICAL finding in that night's report rather
than a dead report; a report that fails to generate entirely is caught the
next night by the heartbeat lens reading the auditor's own run table
(silence is never the signal).

Two honesty rules (audit 2026-09-03, findings 04-F2 and 04-F3): only a lens
that actually ran can resolve its open items, and the report names every
lens that did not run; the checklist is committed to the store only AFTER
the report is on disk, so a failed write or a ``--no-report`` dry run never
consumes a NEW announcement.
"""

from __future__ import annotations

import os
import traceback
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AuditorTenantConfig, load_auditor_tenant
from .findings import Finding
from .ledger_reader import LedgerReader, resolve_ledger_root
from .lenses import LENSES, AuditContext, LensSpec
from .report import render_report, write_report
from .store import AuditorStore


@dataclass
class AuditRunResult:
    tenant: str
    report_path: Path | None
    report_text: str
    new_count: int
    open_count: int
    resolved_count: int
    lens_errors: list[str]


def resolve_store_root(slug: str, store_dir: str | None) -> Path:
    base = store_dir or os.environ.get("AUDITOR_STORE_ROOT") or ".auditor"
    return Path(base).expanduser() / slug


def _local_date(now: datetime, tz_name: str) -> str:
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = UTC
    return now.astimezone(zone).date().isoformat()


@dataclass
class LensPass:
    """What one night's lens pass produced and, as important, what it did
    not: ``completed`` names the lenses whose verdicts are real; ``not_run``
    is the report's own wording for the rest (crashed or skipped)."""

    findings: list[Finding]
    errors: list[str]
    completed: set[str]
    not_run: list[str]


def _run_lenses(lenses: list[LensSpec], ctx: AuditContext, *, local_only: bool) -> LensPass:
    findings: list[Finding] = []
    errors: list[str] = []
    completed: set[str] = set()
    not_run: list[str] = []
    for lens in lenses:
        if local_only and lens.external:
            not_run.append(f"{lens.name} (skipped: --local-only)")
            continue
        try:
            findings.extend(lens.check(ctx))
            completed.add(lens.name)
        except Exception as exc:
            errors.append(f"{lens.name}: {exc}")
            not_run.append(f"{lens.name} (crashed)")
            findings.append(
                Finding(
                    lens="auditor",
                    subject=f"lens {lens.name}",
                    condition="lens-crashed",
                    severity="CRITICAL",
                    detail=f"the {lens.name} lens crashed: {exc} "
                    f"({traceback.format_exc().strip().splitlines()[-1]})",
                )
            )
    return LensPass(findings=findings, errors=errors, completed=completed, not_run=not_run)


def run_audit(
    slug: str,
    *,
    tenants_dir: str | Path | None = None,
    ledger_dir: str | Path | None = None,
    store_dir: str | Path | None = None,
    report_dir: str | Path | None = None,
    lenses: list[LensSpec] | None = None,
    local_only: bool = False,
    write: bool = True,
    now: datetime | None = None,
    tenant_config: AuditorTenantConfig | None = None,
    drafter: object | None = None,
) -> AuditRunResult:
    from .advisory import compute_facts, render_advisory
    from .advisory.draft import draft_counsel, fallback_counsel
    from .advisory.facts import CPA_APPENDIX_STATE_KEY, cpa_appendix_fingerprint, w9_gap
    from .advisory.llm_client import failure_label
    from .advisory.render import LOCAL_ONLY_REASON
    from .lenses import reconcile as reconcile_lens
    from .triage import load_triage

    tenant = tenant_config or load_auditor_tenant(slug, tenants_dir=tenants_dir)
    now = now or datetime.now(UTC)
    now_iso = now.isoformat()
    ledger_root = resolve_ledger_root(slug, str(ledger_dir) if ledger_dir else None)
    store_root = resolve_store_root(slug, str(store_dir) if store_dir else None)
    target_dir = str(report_dir or tenant.report_dir)
    if write and not target_dir:
        raise AuditorReportDirError(
            f"tenant {slug!r} has no [auditor].report_dir and no --report-dir was given"
        )
    report_date = _local_date(now, tenant.timezone)
    # Dated triage entries (snooze, aging) are judged on the tenant's local
    # calendar date, the same date the report carries.
    triage = load_triage(slug, tenants_dir=tenants_dir, as_of=date.fromisoformat(report_date))

    active = LENSES if lenses is None else lenses
    with AuditorStore.open(store_root) as store:
        run_id = store.start_run(slug, now=now_iso)
        try:
            with LedgerReader.open(ledger_root) as reader:
                ctx = AuditContext(
                    tenant=tenant,
                    ledger=reader,
                    now=now,
                    store_root=store_root,
                    tenants_dir=Path(tenants_dir) if tenants_dir else None,
                )
                lens_pass = _run_lenses(active, ctx, local_only=local_only)
                findings, lens_errors = lens_pass.findings, lens_pass.errors
                # Owner triage before the reconcile: muted items vanish (a
                # previously open one resolves visibly once), overrides
                # re-grade proposed severities. Silence accepts. A snooze
                # that expired lets its item through and names the reason
                # so the report can say "(snooze expired)".
                findings = triage.filter(findings)
                reasons = triage.announce_reasons(findings)
                # A clearing whose hand-check card the owner REJECTED closes
                # the reconcile lens's finding same as a recorded one (#368),
                # but the owner's rejection — a fact read off the approval
                # queue, not an engine verdict — replaces the stale WARN
                # detail on the resolved line instead of letting it stand as
                # if nothing was ever said (issue #372). Only meaningful if
                # the lens that owns the fact actually ran tonight.
                resolve_reasons = (
                    reconcile_lens.resolve_notes(ctx)
                    if reconcile_lens.LENS in lens_pass.completed
                    else {}
                )
                # The advisory voice: deterministic facts, then counsel — a
                # failed draft degrades to the fallback voice, a failed facts
                # pass degrades to a one-line notice. The report always ships.
                try:
                    facts = compute_facts(
                        reader,
                        slug=slug,
                        now=now,
                        tenants_dir=tenants_dir,
                        muted_topics=triage.muted_topics,
                        acknowledged=triage.acknowledged,
                    )
                    # The 1099 appendix is a change report (owner decision
                    # 2026-08-22): compare its structural fingerprint against
                    # the store's memory. Unchanged and gap-free, the topic
                    # also leaves the drafter's facts, so the voice stops
                    # re-mentioning a settled picture; a W-9 gap keeps both
                    # the topic and its bullet in view nightly.
                    cpa = facts.get("year-end-cpa") or {}
                    appendix_fp: str | None = cpa_appendix_fingerprint(cpa)
                    appendix_changed = store.get_state(slug, CPA_APPENDIX_STATE_KEY) != appendix_fp
                    w9_gaps = [c for c in cpa.get("form_1099_candidates", []) if w9_gap(c)]
                    drafter_facts = facts
                    if not appendix_changed and not w9_gaps:
                        drafter_facts = {k: v for k, v in facts.items() if k != "year-end-cpa"}
                    # Which model drafts is tenant policy (row 7.12): the
                    # drafter resolves [llm.jobs].draft_advisory itself. Any
                    # failure degrades to the deterministic voice AND names
                    # the reason in the report, so a silently model-less
                    # night is impossible to mistake for counsel.
                    fallback_reason: str | None = None
                    if drafter is not None:
                        counsel = drafter(drafter_facts)  # evals inject a fake
                    elif local_only:
                        counsel = fallback_counsel(drafter_facts)
                        fallback_reason = LOCAL_ONLY_REASON
                    else:
                        try:
                            counsel = draft_counsel(drafter_facts, llm=tenant.raw.get("llm"))
                        except Exception as exc:
                            counsel = fallback_counsel(drafter_facts)
                            fallback_reason = failure_label(exc)
                    advisory = render_advisory(
                        facts,
                        counsel,
                        appendix_changed=appendix_changed,
                        fallback_reason=fallback_reason,
                    )
                except Exception as exc:
                    appendix_fp = None
                    advisory = f"(advisory unavailable this night: {exc})"
            # One transaction: reconcile uncommitted, write the report, then
            # commit. A failed write rolls the checklist back so the next
            # good night still announces tonight's items as NEW; a dry run
            # (write=False) previews and rolls back. Only lenses that ran
            # may resolve their items (verified_lenses).
            result = store.reconcile(
                slug,
                findings,
                now=now_iso,
                verified_lenses=lens_pass.completed,
                commit=False,
                reasons=reasons,
                resolve_reasons=resolve_reasons,
            )
            text = render_report(
                result,
                tenant=slug,
                date=report_date,
                advisory=advisory,
                not_run=lens_pass.not_run,
            )
            if write:
                path = write_report(target_dir, date=report_date, text=text)
                store.commit()
                if appendix_fp is not None:
                    # remember only what actually shipped: a failed write
                    # keeps the next night's full render owed
                    store.set_state(slug, CPA_APPENDIX_STATE_KEY, appendix_fp, now=now_iso)
            else:
                path = None
                store.rollback()
            store.finish_run(
                run_id,
                status="ok",
                now=now_iso,
                report_path=str(path or ""),
                new_count=len(result.new),
                open_count=len(result.open),
                resolved_count=len(result.resolved),
            )
        except Exception as exc:
            store.rollback()  # nothing reconciled tonight is remembered
            store.finish_run(run_id, status="error", now=now_iso, error=str(exc))
            raise
    return AuditRunResult(
        tenant=slug,
        report_path=path,
        report_text=text,
        new_count=len(result.new),
        open_count=len(result.open),
        resolved_count=len(result.resolved),
        lens_errors=lens_errors,
    )


class AuditorReportDirError(ValueError):
    pass
