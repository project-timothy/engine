"""Vulture whitelist: the known FALSE POSITIVES, so CI alarms only on new findings.

Generated 2026-09-18. Every entry below was inspected and is live code:

  * ``payment_records_on`` (18 sites in test_qbo_push_payments.py) is a pytest
    fixture taken as a parameter for its SIDE EFFECT. It monkeypatches
    ``load_tenant`` to flip the payment-records flag on. Deleting the parameter
    would leave those 18 tests passing while no longer testing payment records.
  * ``attrs`` in core/agents/ar/schema.py is required by the
    ``HTMLParser.handle_starttag`` interface signature.
  * ``close_date`` in core/agents/close/evals/test_lock.py is a deliberate
    tripwire: the method raises, and the parameter mirrors the real signature.

Regenerate with::

    uv run vulture core auditor tenants --min-confidence 80 --make-whitelist

and re-inspect anything new before adding it here. A finding is not dead code
until somebody has read it.
"""

payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:232)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:293)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:320)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:346)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:388)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:417)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:442)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:454)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:464)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:497)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:542)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:565)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:589)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:603)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:617)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:639)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:652)
payment_records_on  # unused variable (core/agents/ap/evals/test_qbo_push_payments.py:678)
attrs  # unused variable (core/agents/ar/schema.py:152)
close_date  # unused variable (core/agents/close/evals/test_lock.py:34)
