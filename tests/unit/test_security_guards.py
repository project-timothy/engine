"""The guards the security review prep added (2026-10-13), as code rather
than comments: every web call is https, or plain http only to this machine;
nothing but an ISO date or a digit id reaches a QuickBooks query; nothing but
a known column name reaches an expense-report update."""

from __future__ import annotations

import pytest

from auditor import web as auditor_web
from core.adapters import qbo
from core.agents.expenses import jobs as expenses_jobs
from core.engine import web


@pytest.mark.parametrize(
    "url",
    [
        "https://quickbooks.api.intuit.com/v3/company/1/query",
        "http://127.0.0.1:8770/introspect",
        "http://localhost:4000/v1",
        "http://[::1]:9/x",
    ],
)
def test_https_anywhere_and_plain_http_only_to_this_machine(url):
    assert web.checked(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://example.org/feed.json",
        "http://100.77.135.104:4000/v1",  # another machine, even on the tailnet
        "ftp://example.org/x",
        "//example.org/x",
        "https:///no-host",
    ],
)
def test_everything_else_is_refused_before_a_socket_opens(url):
    with pytest.raises(web.SchemeRefused):
        web.checked(url)
    with pytest.raises(web.SchemeRefused):
        web.request(url)


def test_an_adapter_never_opens_a_local_file():
    with pytest.raises(web.SchemeRefused):
        qbo._default_transport("file:///etc/passwd", data=None, headers={})


def test_the_auditor_keeps_the_same_rule_on_its_own():
    with pytest.raises(ValueError):
        auditor_web.open_url("file:///etc/passwd", timeout=1)
    with pytest.raises(ValueError):
        auditor_web.open_url("http://example.org/", timeout=1)


@pytest.mark.parametrize("bad", ["2026-10-01' OR '1'='1", "2026-13-01", "", None])
def test_only_a_real_iso_date_reaches_a_qbo_query(bad):
    with pytest.raises(ValueError):
        qbo.iso_since(bad)


@pytest.mark.parametrize("bad", ["12' OR Id > '0", "", "12a", "-1"])
def test_only_a_digit_id_reaches_a_qbo_query(bad):
    with pytest.raises(ValueError):
        qbo.qbo_id(bad)
    assert qbo.qbo_id(1234) == "1234"


def test_only_known_columns_reach_an_expense_report_update():
    assert expenses_jobs._sets({"cleared_date": "x"}) == "cleared_date = ?"
    with pytest.raises(ValueError):
        expenses_jobs._sets({"status = 'Paid', total_cents": 0})
