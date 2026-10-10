"""The tenant kit skeleton (issue #430, docs/tenant-kit-design.md).

`engine init <slug> --shape <family>-<size>` renders `authority.toml` and
`kit/` (`brand.toml`, `voice.toml`) beside `tenant.toml`, with the shape's
defaults. Nothing reads the kit yet; doctor reports each part `skip` when it
is absent (a tenant that predates the kit), `ok` when it loads, and `MISSING`
when it is present and malformed, so a broken file never waits for the day a
lane starts reading it.
"""

from __future__ import annotations

import tomllib

import pytest

from core.engine.cli import main as engine_main
from core.engine.config import load_tenant
from core.engine.doctor import run_doctor
from core.engine.init import InitError, init_tenant, render
from core.engine.kit import (
    KIT_FILES,
    SHAPES,
    VOICE_PRESETS,
    KitError,
    family_of,
    load_kit,
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    return tmp_path, root


def _init(root, slug="acme", **kw):
    return init_tenant(slug, root=root, run_audit=False, **kw)


def _kit_lines(report) -> dict[str, tuple[str, str]]:
    return {c.name: (c.status, c.detail) for c in report.checks if c.name.startswith("kit ")}


# ---- the shapes ---------------------------------------------------------------


def test_six_shapes_two_families_three_sizes():
    assert len(SHAPES) == 6
    assert {family_of(s) for s in SHAPES} == {"commercial", "nonprofit"}
    assert {s.split("-", 1)[1] for s in SHAPES} == {"solo", "small", "organization"}


def test_every_shape_renders_a_kit_that_loads(world):
    _tmp, root = world
    for shape in SHAPES:
        slug = f"t-{shape}"
        result = _init(root, slug=slug, shape=shape)
        for name in KIT_FILES.values():
            assert (result.tenant_dir / name).is_file(), f"{shape}: {name}"
        cfg = load_tenant(slug, tenants_root=root)
        assert cfg.identity.shape == shape
        kit = load_kit(result.tenant_dir)
        assert kit.authority["money"]["out"] == "human"
        assert kit.voice["preset"] == VOICE_PRESETS[family_of(shape)]
        assert kit.voice["spelling"] == "en-US"


def test_the_default_shape_is_a_small_business():
    files = render("acme", "A", data_root_rel="acme-data")
    assert tomllib.loads(files["tenant.toml"])["identity"]["shape"] == "commercial-small"
    assert set(KIT_FILES.values()) <= set(files)


def test_a_nonprofit_gets_the_ministry_voice_preset():
    files = render("grace", "C", shape="nonprofit-small", data_root_rel="grace-data")
    assert tomllib.loads(files["kit/voice.toml"])["preset"] == "ministry-conservative-christian"
    commercial = render("acme", "A", shape="commercial-solo", data_root_rel="acme-data")
    assert tomllib.loads(commercial["kit/voice.toml"])["preset"] == "plain-business"


def test_the_brand_carries_no_second_copy_of_the_legal_name():
    """[identity].legal_name is the one source (invariant 10); a second copy
    in brand.toml would drift the first time someone renames the business."""
    files = render("acme", "A", legal_name="Acme Inc.", data_root_rel="acme-data")
    assert "legal_name" not in tomllib.loads(files["kit/brand.toml"])


def test_an_unknown_shape_refuses_and_writes_nothing(world):
    _tmp, root = world
    with pytest.raises(InitError, match="shape"):
        _init(root, shape="megachurch")
    assert not (root / "acme").exists()


def test_the_cli_takes_the_shape(world, capsys):
    _tmp, root = world
    rc = engine_main(
        ["init", "grace", "--archetype", "C", "--shape", "nonprofit-solo", "--root", str(root)]
        + ["--no-audit"]
    )
    assert rc == 0
    assert load_tenant("grace", tenants_root=root).identity.shape == "nonprofit-solo"
    assert "nonprofit-solo" in capsys.readouterr().out


# ---- the loader ---------------------------------------------------------------


def test_a_tenant_without_a_kit_loads_as_empty(tmp_path):
    kit = load_kit(tmp_path)
    assert kit.authority is None and kit.brand is None and kit.voice is None


@pytest.mark.parametrize("value", ['"delegated"', '"agents"', '""'])
def test_the_money_rule_accepts_only_human(tmp_path, value):
    (tmp_path / "authority.toml").write_text(f"[money]\nout = {value}\n", encoding="utf-8")
    with pytest.raises(KitError, match="human"):
        load_kit(tmp_path)


def test_an_authority_file_without_the_money_rule_refuses(tmp_path):
    (tmp_path / "authority.toml").write_text("[people]\n", encoding="utf-8")
    with pytest.raises(KitError, match=r"\[money\]"):
        load_kit(tmp_path)


def test_a_file_that_is_not_toml_refuses_with_its_name(tmp_path):
    (tmp_path / "kit").mkdir()
    (tmp_path / "kit" / "voice.toml").write_text("spelling = \n", encoding="utf-8")
    with pytest.raises(KitError, match="voice.toml"):
        load_kit(tmp_path)


@pytest.mark.parametrize("spelling", ["en-AU", "", "american"])
def test_the_voice_spelling_is_a_known_variety(tmp_path, spelling):
    (tmp_path / "kit").mkdir()
    (tmp_path / "kit" / "voice.toml").write_text(
        f'spelling = "{spelling}"\npreset = "plain-business"\n', encoding="utf-8"
    )
    with pytest.raises(KitError, match="spelling"):
        load_kit(tmp_path)


def test_a_tenant_shape_outside_the_six_does_not_load(world):
    _tmp, root = world
    result = _init(root)
    path = result.tenant_dir / "tenant.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'shape = "commercial-small"', 'shape = "megachurch"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="shape"):
        load_tenant("acme", tenants_root=root)


# ---- doctor -------------------------------------------------------------------


def test_doctor_reports_a_fresh_kit_ok(world):
    _tmp, root = world
    _init(root)
    lines = _kit_lines(run_doctor("acme", tenants_root=root, env={}))
    assert set(lines) == {"kit shape", "kit authority", "kit brand", "kit voice"}
    assert all(status == "ok" for status, _ in lines.values()), lines


def test_doctor_skips_the_kit_of_a_tenant_that_predates_it(world):
    _tmp, root = world
    result = _init(root)
    for name in KIT_FILES.values():
        (result.tenant_dir / name).unlink()
    path = result.tenant_dir / "tenant.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('shape = "commercial-small"\n', ""),
        encoding="utf-8",
    )
    report = run_doctor("acme", tenants_root=root, env={})
    lines = _kit_lines(report)
    assert all(status == "skip" for status, _ in lines.values()), lines
    assert not any(c.name.startswith("kit ") for c in report.missing)


def test_doctor_names_a_broken_kit_file_missing(world):
    _tmp, root = world
    result = _init(root)
    (result.tenant_dir / "authority.toml").write_text('[money]\nout = "agents"\n', encoding="utf-8")
    lines = _kit_lines(run_doctor("acme", tenants_root=root, env={}))
    status, detail = lines["kit authority"]
    assert status == "missing"
    assert "human" in detail


def test_the_config_and_the_kit_name_the_same_six_shapes():
    """The tenant model's Literal and kit.SHAPES are two spellings of one
    list; this keeps them one."""
    import typing

    from core.engine.config import Identity

    annotation = Identity.model_fields["shape"].annotation
    literal = next(a for a in typing.get_args(annotation) if a is not type(None))
    assert typing.get_args(literal) == SHAPES
