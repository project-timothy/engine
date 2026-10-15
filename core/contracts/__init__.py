"""The engine's plug shapes: what an adapter must look like, never what it does.

Apache-2.0, the LICENSE beside this file, unlike the rest of the engine: a
maker who builds a private implement against these shapes keeps it (the
hitch is permissive so any maker's implement fits; the tractor stays AGPL).
Nothing in this package imports behaviour from the rest of ``core``, and
``tests/unit/test_contracts_package.py`` pins that. Four seams: ``mail``
(a mailbox), ``llm`` (a model provider), ``institution`` (where a sending
organization takes a missionary's claims), and ``reader`` (what a receipt's
photo says it cost, a proposal for the person's yes). Jobs and agent runners stay
engine code (docs/decisions/2026-10-10-jobs-and-runners-stay-engine-code.md).
"""
