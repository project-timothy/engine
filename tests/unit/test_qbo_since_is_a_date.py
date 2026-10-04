"""Security review 2026-10-03 (#390, LOW): the QBO query's date is a date.

``invoice_date`` is model-extracted text, and the duplicate guard took the
earliest stored one as ``since`` and interpolated it into
``WHERE TxnDate >= '{since}'``. A crafted date could empty the guard or break
the fetch on every run. Only a real ISO date is ever a ``since`` now, and the
adapter refuses anything else before a query is built.
"""

from __future__ import annotations

import pytest

from core.adapters.qbo import QboClient, iso_since
from core.agents.ap.jobs import earliest_iso_date


@pytest.mark.parametrize(
    "bad", ["2026-01-01' OR '1'='1", "0", "", "2026-13-01", "2026-1-1", "01/02/2026", None]
)
def test_the_adapter_refuses_anything_but_an_iso_date(bad):
    with pytest.raises(ValueError):
        iso_since(bad)


def test_a_real_date_passes():
    assert iso_since("2026-07-01") == "2026-07-01"


def test_both_queries_check_since_before_building_sql(tmp_path):
    client = QboClient(tmp_path / "token.json")
    for call in (client.fetch_evidence, client.fetch_recent_txns):
        with pytest.raises(ValueError):
            call(since="0' OR '1'='1")


def test_the_duplicate_guard_skips_a_crafted_date():
    rows = [
        {"invoice_date": "0' OR '1'='1"},
        {"invoice_date": "2026-08-14"},
        {"invoice_date": None},
        {"invoice_date": "2026-07-30"},
        {"invoice_date": "July 4"},
    ]
    assert earliest_iso_date(rows, default="2026-01-01") == "2026-07-30"
    assert earliest_iso_date([{"invoice_date": "x"}], default="2026-01-01") == "2026-01-01"
