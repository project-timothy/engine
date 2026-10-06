"""The engine's plug shapes: what an adapter must look like, never what it does.

Apache-2.0, the LICENSE beside this file, unlike the rest of the engine: a
maker who builds a private implement against these shapes keeps it (the
hitch is permissive so any maker's implement fits; the tractor stays AGPL).
Nothing in this package imports behaviour from the rest of ``core``, and
``tests/unit/test_mail_seam.py`` pins that. Gate 3 of the extraction plan
(2026-09-21) gathers the remaining shapes here behind the mail seam.
"""
