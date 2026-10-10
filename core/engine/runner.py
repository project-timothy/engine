"""The run framework: ``engine run <tenant> <agent> <job> [--shadow]``.

The runner is the only place that touches the ledger's write path. It:

1. loads the tenant config and resolves the job handler,
2. opens the tenant's git-backed ledger,
3. computes the run's idempotency key and checks for a prior run,
4. on a hit, replays the stored result as a no-op (no work, no commit),
5. otherwise executes the job, persists run + events + queued approvals,
   rebuilds the result from what actually landed (a dropped duplicate event
   or a swallowed approval ask is an anomaly, never a silent loss), and
   commits the ledger with a structured message,
6. if the job raised or reported ``error``, records the failure under a key
   that never replays (``<run key>:failed:<stamp>``) with an
   ``engine.run_failed`` event and a FAILED commit, so the rows the job
   changed before failing have an owner (owner decision 2026-09-03),
7. if the job declares a ``RetryPolicy`` and the failure's cause earns a
   retry, records the attempt as an ``engine.retry`` job record with the
   next attempt's due time (``resume_due`` / ``engine jobs resume`` runs it
   later as the SAME run under the ORIGINAL run key; docs/retries.md), and
8. returns a single structured ``RunResult``.

This is where invariant 3 (every job is safe to re-run) becomes a guarantee
callers can rely on rather than a convention each agent must remember.

``ledger_write_lock`` is the one-ledger-one-writer lock (#136). The runner
takes it around every run; the CLI's own ledger writes (queue approve /
reject, status) take the same lock, so no commit ever sweeps another
process's mid-run rows into its own message.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..ledger import Ledger
from ..llm import telemetry
from ..redact import env_values, redact, redact_text
from .config import MissingFolderError, TenantConfig, load_tenant
from .contracts import JobContext, JobOutput, RetryPolicy
from .guard import ProtectedSurfaceError, WriteGuard
from .registry import UnknownAgentError, UnknownJobError, agent_dir, get_job
from .result import Anomaly, ApprovalNeeded, LlmTotals, RunResult, RunStatus
from .runkey import (
    InputTrail,
    TracedConfig,
    TracedParams,
    UndeclaredInputError,
    audit_mode,
    log_undeclared,
    tracing,
    undeclared,
)

LEDGER_ROOT_ENV = "ENGINE_LEDGER_ROOT"


def resolve_ledger_root(tenant_slug: str, ledger_dir: str | Path | None = None) -> Path:
    """Per-deployment ledger directory for a tenant.

    Explicit ``ledger_dir`` wins, then ``ENGINE_LEDGER_ROOT``, else ``./.ledger``.
    Each tenant gets an isolated git repo under that base.
    """
    if ledger_dir is not None:
        base = Path(ledger_dir)
    elif os.environ.get(LEDGER_ROOT_ENV):
        base = Path(os.environ[LEDGER_ROOT_ENV])
    else:
        base = Path.cwd() / ".ledger"
    return base / tenant_slug


LOCK_ANOMALY = "engine.run_locked"
BUDGET_ANOMALY = "llm.budget"

# Cross-run retries (phase 7 row 7.23). One job record per attempt of a job
# that declares a RetryPolicy; the states a record walks are scheduled (the
# next attempt is due at next_attempt_at) -> resumed (that attempt started;
# its own record follows) -> succeeded | exhausted (terminal, never picked).
RETRY_RECORD = "engine.retry"
RETRY_SCHEDULED = "engine.retry.scheduled"
RETRY_EXHAUSTED = "engine.retry.exhausted"
RETRY_NOT_RETRYABLE = "engine.retry.not_retryable"
RETRY_ATTEMPT = "engine.retry.attempt"
REPORTED_CAUSE = "reported"


@dataclass(frozen=True)
class _Attempt:
    """Which attempt of the run this is: 1 for a first run; N+1 with the
    record of attempt N as parent when a scheduled retry is resumed (or a
    hand re-run joins a pending chain)."""

    number: int = 1
    parent_record_id: int | None = None
    max_attempts: int | None = None

    @property
    def is_retry(self) -> bool:
        return self.number > 1

    def label(self) -> str:
        budget = f" of {self.max_attempts}" if self.max_attempts else ""
        return f"attempt {self.number}{budget}"


FIRST_ATTEMPT = _Attempt()


@dataclass(frozen=True)
class _RetryDecision:
    """What the failure earns: a record in ``state`` (scheduled with a
    delay, or exhausted) plus the anomaly that names it; or no record and
    only the anomaly (cause not retryable, shadow run)."""

    anomaly: Anomaly
    state: str | None = None
    delay_seconds: int | None = None


def _failure_cause(exc: Exception | None) -> tuple[str, bool]:
    """The failure's place in the cause taxonomy (``timeout``,
    ``transport_error``, ``bad_reply``, ...) read off the exception's own
    ``cause`` / ``transient`` attributes, the labels ``ExtractionError`` and
    ``GatewayTransportError`` already carry. A bare exception is
    ``exception``; a reported error (``status="error"``, no exception) is
    ``reported`` and never transient."""
    if exc is None:
        return REPORTED_CAUSE, False
    cause = getattr(exc, "cause", None)
    return (str(cause) if cause else "exception"), bool(getattr(exc, "transient", True))


def _decide_retry(
    policy: RetryPolicy | None,
    attempt: _Attempt,
    *,
    cause: str,
    transient: bool,
    shadow: bool,
    when: str,
) -> _RetryDecision | None:
    if policy is None:
        return None
    failed = f"{attempt.label()} failed ({cause})"
    if shadow:
        return _RetryDecision(
            anomaly=Anomaly(code=RETRY_NOT_RETRYABLE, detail=f"{failed}: shadow runs never retry")
        )
    if not policy.retries(cause) or not transient:
        why = "not in retry_on" if not policy.retries(cause) else "declared not transient"
        return _RetryDecision(
            anomaly=Anomaly(code=RETRY_NOT_RETRYABLE, detail=f"{failed}: {why}; stays FAILED")
        )
    if attempt.number >= policy.max_attempts:
        return _RetryDecision(
            anomaly=Anomaly(
                code=RETRY_EXHAUSTED,
                detail=f"{failed}: retries exhausted at {policy.max_attempts}; stays FAILED",
            ),
            state="exhausted",
        )
    delay = policy.delay_before(attempt.number + 1)
    return _RetryDecision(
        anomaly=Anomaly(
            code=RETRY_SCHEDULED,
            detail=f"{failed}: attempt {attempt.number + 1} of {policy.max_attempts} "
            f"due {delay} s after {when} (engine jobs resume)",
        ),
        state="scheduled",
        delay_seconds=delay,
    )


def _attempt_anomaly(attempt: _Attempt, run_key: str) -> Anomaly:
    return Anomaly(
        code=RETRY_ATTEMPT,
        detail=f"{attempt.label()} of run {run_key} "
        f"(resumed from retry record #{attempt.parent_record_id})",
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _secret_values(tenant: TenantConfig) -> list[tuple[str, str]]:
    """``(variable, value)`` for every secret this tenant DECLARES and this
    host has set: ``[secrets]`` plus each model tier's ``api_key_env`` (row
    7.22). The runner reads them once per run so the redactor can replace a
    value that has no recognisable shape at all, which is most of them."""
    names = list(tenant.secrets.values())
    names += [t.api_key_env for t in tenant.llm.tiers.values() if t.api_key_env]
    return env_values(names)


def _redact_output(output: JobOutput, values: list[tuple[str, str]]) -> JobOutput:
    """Row 7.22: everything a job hands back goes through the redactor once,
    here, before a single row is written.

    One place rather than every job, because a rule that has to be remembered
    at three hundred call sites is a rule that is already broken somewhere.
    What is deliberately NOT redacted is ``EventSpec.key`` and
    ``ApprovalSpec.key``: those are content fingerprints the ledger keys on,
    and moving one would re-park a decided card or re-record an event."""
    return output.model_copy(
        update={
            "summary": redact_text(output.summary, values),
            "actions": [redact_text(a, values) for a in output.actions],
            "events": [
                e.model_copy(update={"payload": redact(e.payload, env_values=values)})
                for e in output.events
            ],
            "approvals": [
                a.model_copy(
                    update={
                        "params": redact(a.params, env_values=values),
                        "reason": redact_text(a.reason, values),
                    }
                )
                for a in output.approvals
            ],
            "anomalies": [
                a.model_copy(update={"detail": redact_text(a.detail, values)})
                for a in output.anomalies
            ],
        }
    )


def _redact_result(result: RunResult, values: list[tuple[str, str]]) -> RunResult:
    """The same rules over a result the runner built itself: an adapter's 401
    body reaches the ledger as a job exception, and the failure trace (#172)
    is durable on purpose."""
    return result.model_copy(
        update={
            "summary": redact_text(result.summary, values),
            "actions": [redact_text(a, values) for a in result.actions],
            "anomalies": [
                a.model_copy(update={"detail": redact_text(a.detail, values)})
                for a in result.anomalies
            ],
            "approvals_needed": [
                a.model_copy(
                    update={
                        "params": redact(a.params, env_values=values),
                        "reason": redact_text(a.reason, values),
                    }
                )
                for a in result.approvals_needed
            ],
        }
    )


def _with_llm(ledger: Ledger, run_key: str, result: RunResult) -> RunResult:
    """The run's model-call totals and budget refusals (row 7.9), read back
    from the ``llm_calls`` rows the policy wrote under this run key. Totals
    go on ``result.llm``; each refusal is an ``llm.budget`` anomaly, so a
    job that caught ``BudgetExceeded`` and carried on still shows it."""
    totals = LlmTotals(**telemetry.run_totals(ledger.conn, run_key))
    refusals = [
        Anomaly(code=BUDGET_ANOMALY, detail=r["detail"] or f"{r['job_type']}: budget refused")
        for r in telemetry.budget_refusals(ledger.conn, run_key)
    ]
    if not refusals and totals == result.llm:
        return result
    return result.model_copy(update={"llm": totals, "anomalies": [*result.anomalies, *refusals]})


class LedgerLocked(RuntimeError):
    """Another process holds the ledger's write lock; the caller refused."""

    def __init__(self, root: Path) -> None:
        self.root = root
        super().__init__(
            f"another run holds the ledger lock at {root}; "
            "refusing to double-execute — re-run when it finishes"
        )


@contextmanager
def ledger_write_lock(root: Path) -> Iterator[None]:
    """One ledger, one writer (#136).

    A launchd run and an interactive run on the same ledger could both pass
    find_run and double-execute — and the ledger's git commit stages
    everything, sweeping the other process's mid-run writes into a
    mis-attributed commit. The same holds for the CLI's own writes (an
    approval taken mid-run). flock dies with the process, so there is no
    stale-lock janitor. Contention raises ``LedgerLocked`` instead of
    waiting: the caller reports a structured refusal and nothing is recorded,
    so the next attempt proceeds normally.
    """
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / ".engine-run.lock", "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LedgerLocked(root) from None
        yield
    finally:
        handle.close()


def _namespace_key(agent: str, job: str, job_key: str) -> str:
    raw = f"{agent}:{job}:{job_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{agent}.{job}.{digest[:16]}"


def _final_status(output: JobOutput) -> RunStatus:
    if output.status == "error":
        return "error"
    if output.approvals:
        return "needs_approval"
    return "ok" if output.status == "ok" else output.status


def _error_result(
    *,
    tenant_slug: str,
    agent: str,
    job: str,
    shadow: bool,
    idempotency_key: str,
    exc: Exception,
) -> RunResult:
    """A structured error result for a job-level failure.

    The failure has no idempotent identity under the run key, so re-running
    after the cause is fixed executes the job instead of replaying the
    failure. Whether it leaves a trace depends on whether the job ran: see
    ``_record_failure``.
    """
    return RunResult(
        tenant=tenant_slug,
        agent=agent,
        job=job,
        status="error",
        shadow=shadow,
        idempotency_key=idempotency_key,
        anomalies=[Anomaly(code="job.exception", detail=f"{type(exc).__name__}: {exc}")],
        summary=(
            f"job failed: {exc}; run `engine doctor {tenant_slug}` for what this host is missing"
            if isinstance(exc, MissingFolderError)
            else f"job failed: {type(exc).__name__}: {exc}"
        ),
    )


def _record_failure(
    ledger: Ledger,
    *,
    run_key: str,
    tenant_slug: str,
    agent: str,
    job: str,
    shadow: bool,
    result: RunResult,
    rows_changed: int,
    policy: RetryPolicy | None = None,
    attempt: _Attempt = FIRST_ATTEMPT,
    params: dict | None = None,
    cause: str = "exception",
    transient: bool = True,
) -> RunResult:
    """A run that executed the job and failed leaves a trace (owner decision
    2026-09-03, honesty audit issue #170; supersedes the 2026-06-10 rule).

    A job with a ``RetryPolicy`` (row 7.23) also gets its attempt on the
    record here, in the same transaction as the FAILED row: an
    ``engine.retry`` job record owned by that row, scheduled (the next
    attempt's due time on it) or exhausted (the budget is spent), plus the
    anomaly that says which. The trace itself is unchanged.

    Domain stores commit as they go, so the rows a job changed before it
    failed are already in the ledger. Before this, they had no run row, no
    event, and no commit of their own: the next unrelated run's ``git add -A``
    swept them into a mis-attributed commit, and the auditor's heartbeat lens
    waited for an error run row the engine never wrote.

    The failure row rides a key that can never be replayed, so the original
    key stays free and the retry executes. A refusal BEFORE the job ran (run
    lock, key computation, strict key audit) still records nothing: nothing
    could have changed.

    Never masks the original error: if the trace itself cannot be written,
    the error result comes back with an ``engine.failure_unrecorded`` anomaly
    on top of the job's own.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    failed_key = f"{run_key}:failed:{stamp}"
    exception = next(
        (a.detail for a in result.anomalies if a.code == "job.exception"), result.summary
    )
    retry = _decide_retry(
        policy, attempt, cause=cause, transient=transient, shadow=shadow, when=failed_key
    )
    traced = result.model_copy(
        update={
            "anomalies": [
                *result.anomalies,
                Anomaly(
                    code="engine.run_failed",
                    detail=f"recorded as {failed_key}; {rows_changed} ledger row change(s) "
                    "before the failure",
                ),
                *([retry.anomaly] if retry else []),
            ]
        }
    )
    try:
        with ledger.atomic():
            stored = ledger.record_run(
                idempotency_key=failed_key,
                tenant=tenant_slug,
                agent=agent,
                job=job,
                status="error",
                shadow=shadow,
                result_json=traced.model_dump_json(),
                summary=result.summary,
            )
            # The job-time records (03-F9) of the run that died belong to
            # its FAILED row: the moves it made before dying have an owner.
            linked = ledger.link_job_records(run_key=run_key, run_id=stored.id)
            ledger.append_event(
                idempotency_key=f"{failed_key}:evt:run_failed",
                run_id=stored.id,
                tenant=tenant_slug,
                agent=agent,
                event_type="engine.run_failed",
                payload={
                    "job": f"{agent}/{job}",
                    "run_key": run_key,
                    "exception": exception,
                    "rows_changed": rows_changed,
                    "job_records": linked,
                    "shadow": shadow,
                },
            )
            if retry is not None and retry.state is not None:
                ledger.record_retry(
                    idempotency_key=f"{run_key}:retry:{attempt.number}",
                    run_key=run_key,
                    run_id=stored.id,
                    tenant=tenant_slug,
                    agent=agent,
                    job=job,
                    record_type=RETRY_RECORD,
                    payload={
                        "run_status": "error",
                        "cause": cause,
                        "exception": exception,
                        "params": dict(params or {}),
                        "shadow": shadow,
                    },
                    retry_policy=policy.model_dump_json() if policy else "",
                    attempt=attempt.number,
                    delay_seconds=retry.delay_seconds,
                    parent_record_id=attempt.parent_record_id,
                    retry_state=retry.state,
                )
        sha = ledger.commit(
            agent=agent,
            job=job,
            idempotency_key=failed_key,
            summary=f"FAILED: {result.summary}",
        )
    except Exception as exc:  # the trace must never hide the job's own failure
        return result.model_copy(
            update={
                "anomalies": [
                    *result.anomalies,
                    Anomaly(
                        code="engine.failure_unrecorded",
                        detail=f"could not record the failed run: {type(exc).__name__}: {exc}",
                    ),
                ]
            }
        )
    return traced.model_copy(update={"commit": sha})


def _replay(stored_result_json: str, created_at: str) -> RunResult:
    """Reconstruct a prior run's result and report it as a no-op."""
    original = RunResult.model_validate_json(stored_result_json)
    return original.model_copy(
        update={
            "status": "noop",
            "commit": None,
            "summary": (
                f"noop (idempotent re-run; original ran {created_at}, "
                f"status={original.status}): {original.summary}"
            ),
        }
    )


SUPERSEDED_STATUS = "superseded"


def _supersede(ledger, ap, tenant_slug: str, agent: str, run_id: int, run_key: str) -> None:
    """Close the older pending cards a job declared this card replaces
    (``ApprovalSpec.supersedes_keys``, proposal 99e8ae57). Called only when a
    card actually holds the new ask, so a swallowed ask retires nothing. Same
    tenant, agent and action type by construction (the subject key carries all
    three); pending rows only; each closure is an event naming both cards."""
    for old_key in ap.supersedes_keys:
        if old_key == ap.key:
            continue
        row = ledger.approval_by_idempotency_key(
            f"apr:{tenant_slug}:{agent}:{ap.action_type}:{old_key}"
        )
        if row is None or row["status"] != "pending":
            continue
        if not ledger.supersede_approval(row["id"]):
            continue
        ledger.append_event(
            idempotency_key=f"{run_key}:apr-superseded:{row['id']}",
            run_id=run_id,
            tenant=tenant_slug,
            agent=agent,
            event_type="engine.approval_superseded",
            payload={
                "action_type": ap.action_type,
                "superseded_id": row["id"],
                "superseded_key": old_key,
                "by_key": ap.key,
            },
        )


def run(
    tenant_slug: str,
    agent: str,
    job: str,
    *,
    shadow: bool = False,
    params: dict | None = None,
    ledger_dir: str | Path | None = None,
    tenants_root: Path | None = None,
) -> RunResult:
    params = params or {}
    tenant: TenantConfig = load_tenant(tenant_slug, tenants_root=tenants_root)
    handler = get_job(agent, job)
    root = resolve_ledger_root(tenant_slug, ledger_dir)

    # The guard is the shadow-safety mechanism: protected production surfaces
    # come from tenant config, and the engine's own data directory must never
    # sit inside one. This check runs for every mode, not just shadow.
    guard = WriteGuard(tenant.ap.protected_paths, allowed=tenant.ap.allowed_paths)
    if guard.is_protected(root):
        raise ProtectedSurfaceError(
            f"ledger root {root} lies inside a protected production surface; "
            "point ENGINE_LEDGER_ROOT/--ledger-dir somewhere safe"
        )

    # One ledger, one run (#136): the refusal is an error result, which is
    # never recorded, so the next run executes normally.
    try:
        with ledger_write_lock(root):
            return _run_locked(
                tenant_slug,
                agent,
                job,
                shadow=shadow,
                params=params,
                tenant=tenant,
                handler=handler,
                root=root,
                guard=guard,
            )
    except LedgerLocked as exc:
        return RunResult(
            tenant=tenant_slug,
            agent=agent,
            job=job,
            status="error",
            shadow=shadow,
            idempotency_key="(unavailable: ledger locked)",
            anomalies=[Anomaly(code=LOCK_ANOMALY, detail=str(exc))],
            summary=f"refused: another run holds the ledger lock at {root}",
        )


def resume_due(
    tenant_slug: str,
    *,
    as_of: datetime | None = None,
    force: bool = False,
    ledger_dir: str | Path | None = None,
    tenants_root: Path | None = None,
) -> list[RunResult]:
    """Execute the tenant's due retries, one at a time under the run lock
    (``engine jobs resume``, row 7.23; a scheduler runs it every 15 minutes).

    A retry is due when its record is ``scheduled`` and ``next_attempt_at``
    has passed ``as_of`` (the clock, injectable; default now). ``force``
    runs every scheduled retry due or not: the owner's ``--now``. Each
    attempt is the original run resumed under its original key, with the
    original params; a held lock is a structured refusal that leaves the
    record scheduled. Returns one result per attempt taken; an empty list
    when nothing was due.
    """
    tenant: TenantConfig = load_tenant(tenant_slug, tenants_root=tenants_root)
    root = resolve_ledger_root(tenant_slug, ledger_dir)
    guard = WriteGuard(tenant.ap.protected_paths, allowed=tenant.ap.allowed_paths)
    if guard.is_protected(root):
        raise ProtectedSurfaceError(
            f"ledger root {root} lies inside a protected production surface; "
            "point ENGINE_LEDGER_ROOT/--ledger-dir somewhere safe"
        )
    stamp = None if force else _iso(as_of or datetime.now(UTC))
    with Ledger.open(root) as ledger:
        due = ledger.due_retries(tenant_slug, as_of=stamp)
    results: list[RunResult] = []
    for record in due:
        taken = _resume_one(record, tenant=tenant, root=root, guard=guard)
        if taken is not None:
            results.append(taken)
    return results


def _resume_one(
    record: dict, *, tenant: TenantConfig, root: Path, guard: WriteGuard
) -> RunResult | None:
    """Take one scheduled attempt under the run lock. None when another
    process took it first (the compare-and-set on the record lost)."""
    tenant_slug, agent, job = record["tenant"], record["agent"], record["job"]
    payload = record["payload"]
    params = dict(payload.get("params") or {})
    shadow = bool(payload.get("shadow", False))
    try:
        with ledger_write_lock(root):
            with Ledger.open(root, repair_event_log=True) as ledger:
                fresh = ledger.retry_record(record["id"])
                if fresh is None or fresh["retry_state"] != "scheduled":
                    return None
                try:
                    handler = get_job(agent, job)
                except (UnknownAgentError, UnknownJobError) as exc:
                    # The job is gone from the code: the chain ends here
                    # rather than erroring every 15 minutes forever.
                    ledger.set_retry_state(record["id"], "exhausted")
                    ledger.commit(
                        agent=agent,
                        job=job,
                        idempotency_key=f"{record['run_key']}:retry-abandoned:{record['attempt']}",
                        summary=f"FAILED: retry abandoned, {exc}",
                    )
                    return RunResult(
                        tenant=tenant_slug,
                        agent=agent,
                        job=job,
                        status="error",
                        shadow=shadow,
                        idempotency_key=record["run_key"],
                        anomalies=[
                            Anomaly(
                                code=RETRY_EXHAUSTED,
                                detail=f"attempt {record['attempt'] + 1} abandoned: {exc}",
                            )
                        ],
                        summary=f"retry abandoned: {exc}",
                    )
                if not ledger.set_retry_state(record["id"], "resumed", expect="scheduled"):
                    return None
            return _run_locked(
                tenant_slug,
                agent,
                job,
                shadow=shadow,
                params=params,
                tenant=tenant,
                handler=handler,
                root=root,
                guard=guard,
                resume=fresh,
            )
    except LedgerLocked as exc:
        return RunResult(
            tenant=tenant_slug,
            agent=agent,
            job=job,
            status="error",
            shadow=shadow,
            idempotency_key=record["run_key"],
            anomalies=[Anomaly(code=LOCK_ANOMALY, detail=str(exc))],
            summary=f"refused: another run holds the ledger lock at {root}",
        )


def _run_locked(  # noqa: C901  # complexity 24, tracked debt
    tenant_slug: str,
    agent: str,
    job: str,
    *,
    shadow: bool,
    params: dict,
    tenant: TenantConfig,
    handler,
    root: Path,
    guard: WriteGuard,
    resume: dict | None = None,
) -> RunResult:
    # Input tracing (#153): under ENGINE_KEY_AUDIT the job sees traced views
    # of its config and params, so every read is recorded and the audit
    # below can prove the run key covers what the run consumed. Production
    # leaves the variable unset and gets the plain objects. A resumed retry
    # (row 7.23) is the original run again under its ORIGINAL key: the key
    # builder does not run, so there is no key trail to audit against.
    auditing = bool(audit_mode()) and resume is None
    budget = handler.retry.max_attempts if handler.retry is not None else None
    # Read outside every tracing() block: this is the RUNNER reading the
    # tenant, not the job, so it must not land in the job's input trail.
    secrets = _secret_values(tenant)
    with Ledger.open(root, repair_event_log=True) as ledger:
        ctx = JobContext(
            tenant=TracedConfig(tenant) if auditing else tenant,  # type: ignore[arg-type]
            tenant_slug=tenant_slug,
            ledger=ledger,
            agent=agent,
            job=job,
            shadow=shadow,
            params=TracedParams(params) if auditing else params,
            agent_dir=agent_dir(agent),
            guard=guard,
        )

        key_trail = InputTrail()
        attempt = _Attempt(max_attempts=budget)
        if resume is not None:
            run_key = resume["run_key"]
            attempt = _Attempt(resume["attempt"] + 1, resume["id"], budget)
        else:
            try:
                with tracing(key_trail):
                    run_key = _namespace_key(agent, job, handler.key(ctx))
            except Exception as exc:  # job-level failure: structured, not a traceback
                return _redact_result(
                    _error_result(
                        tenant_slug=tenant_slug,
                        agent=agent,
                        job=job,
                        shadow=shadow,
                        idempotency_key="(unavailable: key computation failed)",
                        exc=exc,
                    ),
                    secrets,
                )

        prior = ledger.find_run(run_key)
        if prior is not None:
            if resume is not None:
                # The key landed while this attempt waited: nothing to retry.
                ledger.set_retry_state(resume["id"], "succeeded")
            return _replay(prior.result_json, prior.created_at)
        # Job records (03-F9) are namespaced under the run key; the key is
        # known only now, after the job's own key builder ran.
        ctx.run_key = run_key
        if resume is None:
            # A hand re-run while a retry is pending under this key is the
            # next attempt of that chain, never a second chain (row 7.23).
            pending = ledger.scheduled_retry(run_key=run_key)
            if pending is not None and ledger.set_retry_state(
                pending["id"], "resumed", expect="scheduled"
            ):
                attempt = _Attempt(pending["attempt"] + 1, pending["id"], budget)

        run_trail = InputTrail()
        changes_before = ledger.conn.total_changes
        try:
            with tracing(run_trail):
                output = handler.run(ctx)
            # Row 7.22, the one choke point: nothing a job produced reaches a
            # card, an event, the event log or the stored result with a token
            # in it.
            output = _redact_output(output, secrets)
        except Exception as exc:  # job-level failure: structured, not a traceback
            cause, transient = _failure_cause(exc)
            failed = _redact_result(
                _error_result(
                    tenant_slug=tenant_slug,
                    agent=agent,
                    job=job,
                    shadow=shadow,
                    idempotency_key=run_key,
                    exc=exc,
                ),
                secrets,
            )
            if attempt.is_retry:
                failed = failed.model_copy(
                    update={"anomalies": [*failed.anomalies, _attempt_anomaly(attempt, run_key)]}
                )
            return _record_failure(
                ledger,
                run_key=run_key,
                tenant_slug=tenant_slug,
                agent=agent,
                job=job,
                shadow=shadow,
                result=_with_llm(ledger, run_key, failed),
                rows_changed=ledger.conn.total_changes - changes_before,
                policy=handler.retry,
                attempt=attempt,
                params=params,
                cause=cause,
                transient=transient,
            )
        if auditing:
            # The audit (#153): an input the run read that the key neither
            # declared nor read itself is exactly the replay disease — a
            # change to it would replay this result. Strict mode (the test
            # suite) fails the run; nothing is recorded (a harness verdict
            # about the key, never a production run), so the job re-runs
            # once the key is fixed.
            missing = undeclared(key_trail, run_trail)
            log_undeclared(agent, job, missing)
            if missing and audit_mode() == "strict":
                if attempt.is_retry:
                    # A harness verdict records nothing: hand the pending
                    # attempt back so the chain is not left half-taken.
                    ledger.set_retry_state(attempt.parent_record_id, "scheduled", expect="resumed")
                return _error_result(
                    tenant_slug=tenant_slug,
                    agent=agent,
                    job=job,
                    shadow=shadow,
                    idempotency_key=run_key,
                    exc=UndeclaredInputError(
                        f"{agent}/{job} read inputs its run key does not declare: "
                        + ", ".join(missing)
                        + " (fold them into the RunKey or ignore() them with a reason)"
                    ),
                )
        status = _final_status(output)

        result = RunResult(
            tenant=tenant_slug,
            agent=agent,
            job=job,
            status=status,
            shadow=shadow,
            idempotency_key=run_key,
            actions=list(output.actions),
            approvals_needed=[
                ApprovalNeeded(action_type=a.action_type, params=a.params, reason=a.reason)
                for a in output.approvals
            ],
            anomalies=[Anomaly(code=a.code, detail=a.detail) for a in output.anomalies],
            summary=output.summary,
            **({"rubric": output.rubric} if output.rubric is not None else {}),
        )
        if attempt.is_retry:
            result = result.model_copy(
                update={"anomalies": [*result.anomalies, _attempt_anomaly(attempt, run_key)]}
            )
        # Model-call totals and budget refusals ride the result (row 7.9).
        result = _with_llm(ledger, run_key, result)

        # A reported error is treated like a raised one: the failure leaves
        # a trace under a non-replayable key, the run key stays free, and a
        # re-run after the fix executes the job.
        if status == "error":
            cause, transient = _failure_cause(None)
            return _record_failure(
                ledger,
                run_key=run_key,
                tenant_slug=tenant_slug,
                agent=agent,
                job=job,
                shadow=shadow,
                result=result,
                rows_changed=ledger.conn.total_changes - changes_before,
                policy=handler.retry,
                attempt=attempt,
                params=params,
                cause=cause,
                transient=transient,
            )

        # One transaction for run + events + approvals (#136): a crash
        # between them must never leave a recorded run whose cards were
        # never created — that run would replay forever. JSONL flushes
        # after the commit; the locked open above heals a crash between
        # commit and flush.
        with ledger.atomic():
            stored = ledger.record_run(
                idempotency_key=run_key,
                tenant=tenant_slug,
                agent=agent,
                job=job,
                status=status,
                shadow=shadow,
                result_json=result.model_dump_json(),
                summary=output.summary,
            )
            # Defensive: if another process inserted the same key between our
            # find_run and here, replay rather than double-write the side effects.
            if not stored.is_new:
                return _replay(stored.result_json, stored.created_at)
            # Records the job wrote at job time (03-F9) now belong to this run.
            ledger.link_job_records(run_key=run_key, run_id=stored.id)
            if attempt.is_retry:
                # The chain closes: this attempt's record, terminal, owned by
                # the ok run row under the original key (row 7.23).
                ledger.record_retry(
                    idempotency_key=f"{run_key}:retry:{attempt.number}",
                    run_key=run_key,
                    run_id=stored.id,
                    tenant=tenant_slug,
                    agent=agent,
                    job=job,
                    record_type=RETRY_RECORD,
                    payload={"run_status": status, "params": dict(params), "shadow": shadow},
                    retry_policy=handler.retry.model_dump_json() if handler.retry else "",
                    attempt=attempt.number,
                    delay_seconds=None,
                    parent_record_id=attempt.parent_record_id,
                    retry_state="succeeded",
                )

            outcome_anomalies: list[Anomaly] = []
            for ev in output.events:
                new = ledger.append_event(
                    idempotency_key=f"{run_key}:evt:{ev.key}",
                    run_id=stored.id,
                    tenant=tenant_slug,
                    agent=agent,
                    event_type=ev.event_type,
                    payload=ev.payload,
                )
                if not new:
                    # Two EventSpecs in ONE output share a local key: INSERT
                    # OR IGNORE kept the first and dropped this one. A job
                    # bug, but the actions and summary already counted it, so
                    # the drop goes on the record and on the result rather
                    # than vanishing (honesty audit 2026-09-03, F2).
                    ledger.append_event(
                        idempotency_key=f"{run_key}:evt-dropped:{ev.key}",
                        run_id=stored.id,
                        tenant=tenant_slug,
                        agent=agent,
                        event_type="engine.event_dropped",
                        payload={"event_type": ev.event_type, "key": ev.key},
                    )
                    outcome_anomalies.append(
                        Anomaly(
                            code="engine.event_dropped",
                            detail=f"{ev.event_type} under key {ev.key!r}: duplicate event key "
                            "in this run; the second copy was not recorded",
                        )
                    )
            honored: list[ApprovalNeeded] = []
            for ap in output.approvals:
                # Stable subject key, NOT run-namespaced (#136): a job's ap.key
                # is a content fingerprint (md5, qbo id set, report content), so
                # the same still-flagged subject re-parks NOTHING on later runs —
                # the queued card (or its recorded resolution) is the memory. A
                # flow that wants a deliberate re-ask varies its ap.key.
                subject_key = f"apr:{tenant_slug}:{agent}:{ap.action_type}:{ap.key}"
                queued = ledger.enqueue_approval(
                    idempotency_key=subject_key,
                    run_id=stored.id,
                    tenant=tenant_slug,
                    agent=agent,
                    action_type=ap.action_type,
                    params=ap.params,
                )
                if queued:
                    honored.append(
                        ApprovalNeeded(
                            action_type=ap.action_type, params=ap.params, reason=ap.reason
                        )
                    )
                    _supersede(ledger, ap, tenant_slug, agent, stored.id, run_key)
                    continue
                # Deduped. Against a pending card that is the designed
                # memory (the card exists, the ask is honored); against a
                # RESOLVED card the job's fresh ask was swallowed while its
                # summary claims a park (issue #161, live 2026-09-01) — put
                # the swallow on the record so the flow's missing key
                # variation is visible.
                existing = ledger.approval_by_idempotency_key(subject_key)
                if existing is None or existing["status"] == "pending":
                    honored.append(
                        ApprovalNeeded(
                            action_type=ap.action_type, params=ap.params, reason=ap.reason
                        )
                    )
                    _supersede(ledger, ap, tenant_slug, agent, stored.id, run_key)
                    continue
                ledger.append_event(
                    idempotency_key=f"{run_key}:apr-swallowed:{ap.key}",
                    run_id=stored.id,
                    tenant=tenant_slug,
                    agent=agent,
                    event_type="engine.approval_swallowed",
                    payload={
                        "action_type": ap.action_type,
                        "key": ap.key,
                        "existing_id": existing["id"],
                        "existing_status": existing["status"],
                    },
                )
                outcome_anomalies.append(
                    Anomaly(
                        code="engine.approval_swallowed",
                        detail=f"{ap.action_type} ask {ap.key!r} not parked: card "
                        f"#{existing['id']} already holds that key ({existing['status']}); "
                        "the flow needs a key variation to ask again",
                    )
                )

            # Outcome, not intent (honesty audit 2026-09-03, F4): the result
            # stored above was composed before anything landed. Rebuild it
            # from what did: approvals_needed lists the asks a card actually
            # holds, the status needs approval only when one does, and the
            # stored row says the same thing a --json consumer sees.
            if outcome_anomalies or len(honored) != len(output.approvals):
                if honored:
                    status = "needs_approval"
                elif status == "needs_approval":
                    status = "ok"
                result = result.model_copy(
                    update={
                        "status": status,
                        "approvals_needed": honored,
                        "anomalies": [*result.anomalies, *outcome_anomalies],
                    }
                )
                ledger.conn.execute(
                    "UPDATE runs SET status = ?, result_json = ? WHERE id = ?",
                    (status, result.model_dump_json(), stored.id),
                )

        sha = ledger.commit(
            agent=agent,
            job=job,
            idempotency_key=run_key,
            summary=output.summary or status,
        )
        # The commit SHA describes this invocation, not the stored result, so
        # it lives on the returned object only (a replay performs no commit).
        return result.model_copy(update={"commit": sha})
