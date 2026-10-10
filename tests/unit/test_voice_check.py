"""`engine voice-check <tenant> <file> [--register NAME]` (issue #431,
docs/tenant-kit-design.md section 2).

Deterministic, no model call: the tenant's `kit/voice.toml` (spelling,
preset, banned words and shapes, glossary, registers) checked against a file,
one line per hit with the fix. Exit 0 clean, 1 warnings, 2 fatal.
"""

from __future__ import annotations

import pytest

from core.engine.cli import main as engine_main
from core.engine.init import init_tenant
from core.engine.kit import KitError, load_kit
from core.voice import PRESETS, VoiceError, check_text, exit_code, load_preset

US = {"spelling": "en-US", "preset": "custom"}
GB = {"spelling": "en-GB", "preset": "custom"}


def _checks(hits) -> list[str]:
    return [h.check for h in hits]


# ---- spelling --------------------------------------------------------------------


@pytest.mark.parametrize(
    "word",
    ["colour", "behaviour", "organisation", "centre", "travelled", "defence", "grey", "whilst"],
)
def test_us_spelling_flags_british_forms(word):
    hits = check_text(f"The {word} matters.", US)
    assert _checks(hits) == ["spelling"] and hits[0].severity == "fatal"


@pytest.mark.parametrize(
    "text",
    [
        "I promise the hour's exercise is wise.",
        "The analysis covers four colors and our behavior.",
        "Their premise and expertise survive any compromise.",
        "We advertise, revise and supervise.",
    ],
)
def test_us_spelling_never_flags_words_that_only_look_british(text):
    assert check_text(text, US) == []


@pytest.mark.parametrize("word", ["color", "behavior", "center", "traveled", "defense", "gray"])
def test_gb_spelling_flags_american_forms(word):
    assert _checks(check_text(f"The {word} matters.", GB)) == ["spelling"]


def test_gb_spelling_leaves_oxford_ize_alone():
    """-ize is correct Oxford British; a UK tenant that writes it is right."""
    assert check_text("We organize and recognize it.", GB) == []


def test_a_hit_names_the_line_and_the_fix():
    hits = check_text("first line\nthe colour here\n", US)
    assert hits[0].line == 2
    assert "-our" in hits[0].message


# ---- banned words and shapes -------------------------------------------------------


def test_a_banned_word_is_fatal_and_whole_word_only():
    voice = {**US, "banned": ["synergy"]}
    assert _checks(check_text("Real synergy here.", voice)) == ["banned"]
    assert check_text("Synergyx is a made-up name.", voice) == []


def test_a_banned_shape_is_a_regular_expression():
    voice = {**US, "banned_patterns": ["—"]}
    hits = check_text("One thing — then another.", voice)
    assert _checks(hits) == ["banned-pattern"]


def test_the_preset_brings_its_own_banned_words():
    voice = {"spelling": "en-US", "preset": "plain-business"}
    assert "banned" in _checks(check_text("Let us delve into it.", voice))


# ---- glossary ---------------------------------------------------------------------


def test_the_glossary_says_which_term_to_use():
    voice = {**US, "glossary": {"invoice": ["bill", "statement of charges"]}}
    hits = check_text("Send the bill today.", voice)
    assert _checks(hits) == ["glossary"] and hits[0].severity == "warn"
    assert "invoice" in hits[0].message


# ---- registers ---------------------------------------------------------------------

REGISTERS = {"registers": {"informal": {"contractions": True}, "formal": {"contractions": False}}}


def test_formal_register_refuses_contractions():
    voice = {**US, **REGISTERS}
    hits = check_text("We don’t ship it's parts.", voice, register="formal")
    assert _checks(hits) == ["contraction", "contraction"]
    assert all(h.severity == "fatal" for h in hits)


def test_formal_register_allows_possessives():
    voice = {**US, **REGISTERS}
    assert check_text("The company's report and Pat's note.", voice, register="formal") == []


def test_informal_register_flags_stiff_phrases():
    voice = {**US, **REGISTERS}
    hits = check_text("I am fixing it and it is small.", voice, register="informal")
    assert _checks(hits) == ["stiff", "stiff"]
    assert all(h.severity == "warn" for h in hits)


def test_no_register_named_skips_the_register_rules():
    voice = {**US, **REGISTERS}
    assert check_text("I am sure we don't mind.", voice) == []


def test_an_unknown_register_refuses():
    with pytest.raises(VoiceError, match="memo"):
        check_text("text", {**US, **REGISTERS}, register="memo")


# ---- exit codes and presets ----------------------------------------------------------


def test_exit_codes():
    assert exit_code([]) == 0
    assert exit_code(check_text("Send the bill.", {**US, "glossary": {"invoice": ["bill"]}})) == 1
    assert exit_code(check_text("The colour.", US)) == 2


def test_both_presets_ship_and_the_ministry_one_says_who_writes_it():
    assert set(PRESETS) == {"plain-business", "ministry-conservative-christian"}
    ministry = load_preset("ministry-conservative-christian")
    assert "ministry partner" in ministry["frame"]
    assert load_preset("plain-business")["frame"]


def test_an_unknown_preset_does_not_load(tmp_path):
    (tmp_path / "kit").mkdir()
    (tmp_path / "kit" / "voice.toml").write_text(
        'spelling = "en-US"\npreset = "pirate"\n', encoding="utf-8"
    )
    with pytest.raises(KitError, match="pirate"):
        load_kit(tmp_path)


def test_a_banned_pattern_that_does_not_compile_does_not_load(tmp_path):
    (tmp_path / "kit").mkdir()
    (tmp_path / "kit" / "voice.toml").write_text(
        'spelling = "en-US"\npreset = "custom"\nbanned_patterns = ["(unclosed"]\n',
        encoding="utf-8",
    )
    with pytest.raises(KitError, match="unclosed"):
        load_kit(tmp_path)


# ---- the command ----------------------------------------------------------------------


@pytest.fixture
def tenant(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    init_tenant("acme", root=root, run_audit=False)
    return tmp_path, root


def test_the_command_prints_each_hit_and_exits_fatal(tenant, capsys):
    tmp, root = tenant
    draft = tmp / "draft.txt"
    draft.write_text("Our colour scheme.\n", encoding="utf-8")
    rc = engine_main(["voice-check", "acme", str(draft), "--root", str(root)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "spelling" in out and "line 1" in out and "RESULT" in out


def test_the_command_exits_clean_on_a_clean_file(tenant, capsys):
    tmp, root = tenant
    draft = tmp / "draft.txt"
    draft.write_text("Our color scheme is ready.\n", encoding="utf-8")
    assert engine_main(["voice-check", "acme", str(draft), "--root", str(root)]) == 0


def test_the_command_takes_a_register(tenant, capsys):
    tmp, root = tenant
    draft = tmp / "draft.txt"
    draft.write_text("We don't ship on Fridays.\n", encoding="utf-8")
    args = ["voice-check", "acme", str(draft), "--root", str(root)]
    assert engine_main([*args, "--register", "informal"]) == 0
    assert engine_main([*args, "--register", "formal"]) == 2


def test_the_command_refuses_a_tenant_without_a_voice(tenant, capsys):
    tmp, root = tenant
    (root / "acme" / "kit" / "voice.toml").unlink()
    draft = tmp / "draft.txt"
    draft.write_text("Hello.\n", encoding="utf-8")
    rc = engine_main(["voice-check", "acme", str(draft), "--root", str(root)])
    assert rc == 2
    assert "voice.toml" in capsys.readouterr().err
