"""PN canonicalizer evals (ported from the retired bookkeeper-auditor's
projects.py, 2026-09-04): four written forms resolve to PYY_NNNN, nicknames
come from tenant config, sentinels for overhead and multi-project rows."""

from __future__ import annotations

from auditor.pn import MULTI, OVERHEAD, CodeFormat, canonicalize, find_pns

# The first tenant's [books.cost_object] (#340): the scheme is tenant data.
P = CodeFormat(r"(?i)p\s?n?\s?(\d{2})\s?_?\s?(\d{4})", "P{0}_{1}")

NICKNAMES = {
    "widget 0101": "P00_0101",
    "gadget 0104": "P00_0104",
    "abc 6": "P00_0105",
    "abc-6": "P00_0105",
    "pn 0106": "P00_0106",
    "sprocket": "P00_0107",
}


def test_already_canonical_p_numbers():
    assert canonicalize("P00_0101", code=P) == "P00_0101"
    assert canonicalize("P00_0105", code=P) == "P00_0105"


def test_p_number_with_trailing_nickname():
    assert canonicalize("P00_0108 Sprocket", code=P) == "P00_0108"
    assert canonicalize("P00_0109 Widget Testing", code=P) == "P00_0109"


def test_p_number_with_leading_pn_prefix():
    """`PN00_0103` (the house gl_account spelling) resolves cleanly."""
    assert canonicalize("PN00_0103", code=P) == "P00_0103"


def test_p_number_with_parenthetical_nickname():
    assert canonicalize("P00_0101 (Widget 0101)", code=P) == "P00_0101"
    assert canonicalize("P00_0110 (Sunset, legacy)", code=P) == "P00_0110"


def test_p_number_with_space_instead_of_underscore():
    assert canonicalize("p00 0111", code=P) == "P00_0111"
    assert canonicalize("p 00 _ 0101", code=P) == "P00_0101"


def test_nickname_aliases_come_from_config():
    assert canonicalize("Widget 0101", nicknames=NICKNAMES, code=P) == "P00_0101"
    assert canonicalize("Gadget 0104", nicknames=NICKNAMES, code=P) == "P00_0104"
    assert canonicalize("ABC 6", nicknames=NICKNAMES, code=P) == "P00_0105"
    assert canonicalize("ABC-6", nicknames=NICKNAMES, code=P) == "P00_0105"
    assert canonicalize("PN 0106", nicknames=NICKNAMES, code=P) == "P00_0106"
    assert canonicalize("Sprocket", nicknames=NICKNAMES, code=P) == "P00_0107"
    assert canonicalize("Widget 0101", code=P) is None  # no config, no nickname


def test_nickname_case_and_whitespace_insensitive():
    assert canonicalize("  widget   0101 ", nicknames=NICKNAMES, code=P) == "P00_0101"
    assert canonicalize("WIDGET 0101", nicknames={"Widget  0101": "P00_0101"}, code=P) == "P00_0101"


def test_overhead_sentinel():
    assert canonicalize("General/Overhead", code=P) == OVERHEAD
    assert canonicalize("general / overhead", code=P) == OVERHEAD
    assert canonicalize("Overhead", code=P) == OVERHEAD
    assert canonicalize("G&A", overhead_tokens=["g&a"], code=P) == OVERHEAD


def test_multi_sentinel():
    assert canonicalize("Multi (Widget 0101, ABC-6, Sprocket x2)", code=P) == MULTI
    assert canonicalize("multi", code=P) == MULTI


def test_unresolvable_returns_none():
    assert canonicalize("Some Random Project Name", code=P) is None
    assert canonicalize("Pre-Prep Elimination", code=P) is None


def test_empty_and_none_inputs():
    assert canonicalize(None, code=P) is None
    assert canonicalize("", code=P) is None
    assert canonicalize("   ", code=P) is None


def test_short_pn_does_not_match_the_regex():
    """`PN 0106` has four digits, no year: the alias map, never the regex."""
    assert canonicalize("PN 0106", code=P) is None
    assert canonicalize("PN 0106", nicknames=NICKNAMES, code=P) == "P00_0106"


def test_find_pns_pulls_every_p_number_out_of_free_text():
    assert find_pns("Cost of Goods Sold:Project Expense - PN00_0101", P) == ["P00_0101"]
    assert find_pns("Cost of Goods Sold:Project Expense", P) == []
    assert find_pns("P00_0107 and p00 0112", P) == ["P00_0107", "P00_0112"]
    assert find_pns("", P) == []


def test_a_tenant_without_a_scheme_resolves_no_code_but_still_its_nicknames():
    assert find_pns("Project Expense - PN00_0101") == []
    assert canonicalize("P00_0101") is None
    assert canonicalize("sprocket", nicknames=NICKNAMES) == "P00_0107"
    assert canonicalize("overhead") == OVERHEAD
