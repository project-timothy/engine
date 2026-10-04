"""Bank-rec evals: the statement anchor and feed acceptance (checks 2 + 3).

Design correction pinned here (2026-07-21): this book is feed-driven, so
register and statement are BOTH cleared-only views and the anchor is
straight equality. Committed-not-cleared checks render as context, never
as a reconciling term.
"""

from __future__ import annotations

from core.agents.close import checks as close_checks
from core.agents.close import jobs as close_jobs
from core.agents.close.checks import CloseContext
from core.agents.close.schema import CheckResult

from .test_preflight import FakeQbo, _invoice, _ledger

BANK = "5/3 Business Checking"


def _balance_sheet(account_cell, amount_text):
    return {
        "Rows": {
            "Row": [
                {
                    "Header": {"ColData": [{"value": "ASSETS"}]},
                    "Rows": {
                        "Row": [
                            {
                                "ColData": [
                                    {"value": account_cell},
                                    {"value": amount_text},
                                ],
                                "type": "Data",
                            }
                        ]
                    },
                    "type": "Section",
                }
            ]
        }
    }


def _ctx(tmp_path, ledger, *, qbo=None, statement_cents=None, bank=BANK):
    return CloseContext(
        tenant_slug="t",
        ledger=ledger,
        month="2026-07",
        qbo=qbo or FakeQbo(),
        auditor_store=tmp_path / "aud" / "t" / "auditor.sqlite3",
        uncategorized_block_over_cents=50_000,
        bank_account=bank,
        statement_balance_cents=statement_cents,
    )


def test_matching_balances_anchor_ok(tmp_path):
    with _ledger(tmp_path) as ledger:
        qbo = FakeQbo(reports={"BalanceSheet": _balance_sheet(BANK, "118,687.12")})
        result = close_checks.check_statement_anchor(
            _ctx(tmp_path, ledger, qbo=qbo, statement_cents=11868712)
        )
    assert result.status == "OK"
    assert "$118,687.12" in result.summary


def test_delta_blocks_with_the_amount(tmp_path):
    with _ledger(tmp_path) as ledger:
        qbo = FakeQbo(reports={"BalanceSheet": _balance_sheet(BANK, "118,687.12")})
        result = close_checks.check_statement_anchor(
            _ctx(tmp_path, ledger, qbo=qbo, statement_cents=11900000)
        )
    assert result.status == "BLOCK"
    assert "delta $312.88" in result.summary


def test_missing_statement_balance_warns(tmp_path):
    with _ledger(tmp_path) as ledger:
        result = close_checks.check_statement_anchor(_ctx(tmp_path, ledger))
    assert result.status == "WARN"
    assert "--statement-balance" in result.summary


def test_unfindable_account_blocks(tmp_path):
    with _ledger(tmp_path) as ledger:
        qbo = FakeQbo(reports={"BalanceSheet": _balance_sheet("Some Other Account", "1.00")})
        result = close_checks.check_statement_anchor(
            _ctx(tmp_path, ledger, qbo=qbo, statement_cents=100)
        )
    assert result.status == "BLOCK"
    assert "bank_account" in result.summary


def test_committed_checks_render_as_context_not_delta(tmp_path):
    with _ledger(tmp_path) as ledger:
        _invoice(
            ledger,
            status="Scheduled in bill pay",
            check_ref="9065",
            amount_cents=331200,
            number="20857",
        )
        qbo = FakeQbo(reports={"BalanceSheet": _balance_sheet(BANK, "118,687.12")})
        result = close_checks.check_statement_anchor(
            _ctx(tmp_path, ledger, qbo=qbo, statement_cents=11868712)
        )
    # equality holds WITH a check in the mail: the register never saw it
    assert result.status == "OK"
    assert "travelling" in result.summary
    assert "9065" in " ".join(result.details)


def test_feed_acceptance_follows_the_anchor(tmp_path):
    ok = CheckResult(name="statement-anchor", status="OK", summary="s")
    warn = CheckResult(name="statement-anchor", status="WARN", summary="s")
    block = CheckResult(name="statement-anchor", status="BLOCK", summary="s")
    with _ledger(tmp_path) as ledger:
        ctx = _ctx(tmp_path, ledger)
        assert close_checks.check_feed_acceptance(ctx, ok).status == "OK"
        assert close_checks.check_feed_acceptance(ctx, warn).status == "WARN"
        blocked = close_checks.check_feed_acceptance(ctx, block)
    assert blocked.status == "BLOCK"
    assert "Banking tab" in blocked.summary


def test_key_varies_with_statement_balance(tmp_path):
    # Live lesson 2026-07-21: two same-minute preflights with different
    # balances must never share a key, or the second replays the first's
    # stale verdict.
    from core.engine.config import TenantConfig
    from core.engine.contracts import JobContext

    # The key declares the close section (#153), so it needs a real config.
    tenant = TenantConfig.model_validate({"identity": {"legal_name": "T", "slug": "t"}})
    a = JobContext(tenant=tenant, tenant_slug="t", ledger=None, agent="close", job="preflight")
    a.params = {"month": "2026-07", "statement_balance": "1.00"}
    b = JobContext(tenant=tenant, tenant_slug="t", ledger=None, agent="close", job="preflight")
    b.params = {"month": "2026-07", "statement_balance": "2.00"}
    assert close_jobs._preflight_key(a) != close_jobs._preflight_key(b)


def test_statement_balance_param_parses_dollars(tmp_path, monkeypatch):
    from core.engine.contracts import JobContext

    ctx = JobContext(tenant=None, tenant_slug="t", ledger=None, agent="close", job="preflight")
    ctx.params = {"statement_balance": "$118,687.12"}
    assert close_jobs._statement_balance_cents(ctx) == 11868712
    ctx.params = {}
    assert close_jobs._statement_balance_cents(ctx) is None
    ctx.params = {"statement_balance": "a lot"}
    try:
        close_jobs._statement_balance_cents(ctx)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
