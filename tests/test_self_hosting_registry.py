"""Self-hosting bug 1: never evict the running jaunt package during discovery;
preserve self registrations across registry clears.

These tests pin the carve-out (``evict_modules_for_import`` never drops the
running framework's own modules) and the preservation semantics
(``clear_registries(preserve_modules=...)`` +
``prepare_import_environment``), scoped to discovered ∩ imported ∩ self so
self-specs never leak into an adopter build.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Generator
from pathlib import Path

import pytest

from jaunt import discovery
from jaunt.registry import (
    ModuleMagicDefaults,
    SpecEntry,
    clear_registries,
    get_contract_registry,
    get_magic_registry,
    get_module_magic_registry,
    get_test_registry,
    register_contract,
    register_magic,
    register_module_magic,
    register_test,
)
from jaunt.spec_ref import normalize_spec_ref


@pytest.fixture(autouse=True)
def _clean() -> Generator[None, None, None]:
    clear_registries()
    yield
    clear_registries()


def _entry(kind: str, module: str, qualname: str) -> SpecEntry:
    return SpecEntry(
        kind=kind,  # type: ignore[arg-type]
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file="/fake.py",
        obj=object(),
        decorator_kwargs={},
    )


# ---------------------------------------------------------------------------
# Group 1 — clear_registries(preserve_modules=...)
# ---------------------------------------------------------------------------


def test_clear_registries_preserve_keeps_matching_across_all_registries() -> None:
    register_magic(_entry("magic", "keep.me", "A"))
    register_magic(_entry("magic", "drop.me", "B"))
    register_test(_entry("test", "keep.me", "T"))
    register_test(_entry("test", "drop.me", "T2"))
    register_contract(_entry("contract", "keep.me", "C"))
    register_contract(_entry("contract", "drop.me", "C2"))
    register_module_magic(
        ModuleMagicDefaults(module="keep.me", source_file="k.py", decorator_kwargs={})
    )
    register_module_magic(
        ModuleMagicDefaults(module="drop.me", source_file="d.py", decorator_kwargs={})
    )

    clear_registries(preserve_modules=frozenset({"keep.me"}))

    assert {e.module for e in get_magic_registry().values()} == {"keep.me"}
    assert {e.module for e in get_test_registry().values()} == {"keep.me"}
    assert {e.module for e in get_contract_registry().values()} == {"keep.me"}
    assert set(get_module_magic_registry().keys()) == {"keep.me"}


def test_clear_registries_default_clears_everything_byte_for_byte() -> None:
    register_magic(_entry("magic", "a.b", "A"))
    register_test(_entry("test", "a.b", "T"))
    register_contract(_entry("contract", "a.b", "C"))
    register_module_magic(
        ModuleMagicDefaults(module="a.b", source_file="x.py", decorator_kwargs={})
    )

    clear_registries()

    assert get_magic_registry() == {}
    assert get_test_registry() == {}
    assert get_contract_registry() == {}
    assert get_module_magic_registry() == {}


def test_clear_registries_empty_preserve_is_total() -> None:
    register_magic(_entry("magic", "a.b", "A"))
    register_module_magic(
        ModuleMagicDefaults(module="a.b", source_file="x.py", decorator_kwargs={})
    )

    clear_registries(preserve_modules=frozenset())

    assert get_magic_registry() == {}
    assert get_module_magic_registry() == {}


# ---------------------------------------------------------------------------
# Group 2 — eviction carve-out (self package never evicted)
# ---------------------------------------------------------------------------


def test_is_self_module_matches_running_package() -> None:
    assert discovery.is_self_module("jaunt")
    assert discovery.is_self_module("jaunt.discovery")
    assert not discovery.is_self_module("jaunttools")
    assert not discovery.is_self_module("app.specs")


def test_evict_carve_out_file_rule_preserves_self_evicts_planted() -> None:
    import jaunt
    import jaunt.discovery  # noqa: F401 - ensure a self submodule is live

    src_root = Path(jaunt.__file__).resolve().parent
    assert "jaunt.discovery" in sys.modules

    planted = types.ModuleType("planted_notself_xyz")
    planted.__file__ = str(src_root / "planted_notself_xyz.py")
    sys.modules["planted_notself_xyz"] = planted

    # Snapshot self modules so a broken carve-out cannot destabilize the session.
    saved = {k: v for k, v in sys.modules.items() if k == "jaunt" or k.startswith("jaunt.")}
    try:
        # module_names empty -> only the __file__-under-roots rule fires.
        discovery.evict_modules_for_import(module_names=[], roots=[src_root])
        assert "jaunt" in sys.modules
        assert "jaunt.discovery" in sys.modules
        assert "planted_notself_xyz" not in sys.modules
    finally:
        for k, v in saved.items():
            sys.modules.setdefault(k, v)
        sys.modules.pop("planted_notself_xyz", None)


def test_evict_carve_out_exact_and_prefix_rule_preserves_self(tmp_path: Path) -> None:
    import jaunt
    import jaunt.discovery  # noqa: F401

    saved = {k: v for k, v in sys.modules.items() if k == "jaunt" or k.startswith("jaunt.")}
    try:
        # roots is an empty tmp dir -> only the exact/prefix rules can fire.
        discovery.evict_modules_for_import(module_names=["jaunt"], roots=[tmp_path])
        assert "jaunt" in sys.modules
        assert "jaunt.discovery" in sys.modules
    finally:
        for k, v in saved.items():
            sys.modules.setdefault(k, v)


def test_self_preserved_modules_scoped_to_discovered_imported_self() -> None:
    import jaunt.discovery  # noqa: F401

    names = ["jaunt.discovery", "jaunt.not_imported_xyz", "app.specs"]
    preserved = discovery.self_preserved_modules(names)
    assert "jaunt.discovery" in preserved
    assert "jaunt.not_imported_xyz" not in preserved  # self but not imported
    assert "app.specs" not in preserved  # imported-or-not but not self


# ---------------------------------------------------------------------------
# Group 3 — split-brain regression pin (integration; wave-ordered skip)
# ---------------------------------------------------------------------------


def _has_top_level_magic_module_call(src: str) -> bool:
    """True only for a genuine ``jaunt.magic_module(...)`` module statement.

    Uses a top-level AST scan so string-literal templates (init_template.py) and
    the ``magic_module`` implementation itself never count as governed specs.
    """
    import ast

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if fname == "magic_module":
                return True
    return False


def test_specs_reports_self_governed_modules(capsys: pytest.CaptureFixture[str]) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    if not (repo_root / "jaunt.toml").exists():
        pytest.skip("root jaunt.toml not present yet (self-hosting wave 2)")
    src = repo_root / "src" / "jaunt"
    has_self_specs = any(
        _has_top_level_magic_module_call(p.read_text(encoding="utf-8", errors="ignore"))
        for p in src.rglob("*.py")
        if "__generated__" not in p.parts
    )
    if not has_self_specs:
        pytest.skip("no jaunt-governed self modules yet (self-hosting waves 3-5)")

    from jaunt import cli

    rc = cli.main(["specs", "--json", "--root", str(repo_root)])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert rc == 0
    assert data["ok"] is True
    modules = {s["module"] for s in data["specs"]}
    # Pre-fix (registry split-brain) this set is empty; post-fix it names self modules.
    assert any(discovery.is_self_module(m) for m in modules)


# ---------------------------------------------------------------------------
# Group 4 — leak pin (self specs must not survive an adopter clear)
# ---------------------------------------------------------------------------


def test_self_specs_do_not_leak_into_adopter_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate the post-conversion tester path: a self magic module already
    # imported AND its spec registered before adopter discovery runs.
    import jaunt.heldout  # noqa: F401

    register_magic(_entry("magic", "jaunt.heldout", "make_report"))
    assert "jaunt.heldout" in sys.modules
    assert any(e.module == "jaunt.heldout" for e in get_magic_registry().values())

    # Adopter project: discovery yields only adopter names, never jaunt.*.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "specs.py").write_text(
        "import jaunt\n\njaunt.magic_module(__name__)\n\n\ndef f() -> int:\n    ...\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    adopter_mods = discovery.discover_modules(
        roots=[tmp_path], exclude=[], generated_dir="__generated__"
    )
    assert adopter_mods == ["app.specs"]
    assert not any(discovery.is_self_module(m) for m in adopter_mods)

    # The one shared entry point: preserve set must be empty here (nothing
    # discovered is self), so the clear stays total and jaunt.heldout is dropped.
    discovery.prepare_import_environment(module_names=adopter_mods, roots=[tmp_path])
    discovery.import_and_collect(adopter_mods, kind="magic")

    snapshot = dict(get_magic_registry())
    assert not any(discovery.is_self_module(e.module) for e in snapshot.values())
    assert any(e.module == "app.specs" for e in snapshot.values())


# ---------------------------------------------------------------------------
# Group 5 — second-pass pin (preservation, not just carve-out)
# ---------------------------------------------------------------------------

_SELF_FIXTURE = '''
import jaunt

jaunt.magic_module(__name__)


def compute(x: int) -> int:
    """Return x doubled."""
    ...
'''


def test_second_pass_preserves_self_specs_across_reimport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "selfpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "logic.py").write_text(_SELF_FIXTURE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    # Treat the fixture package as the "running framework" for is_self_module.
    monkeypatch.setattr(discovery, "_SELF_PACKAGE", "selfpkg")

    for mod in ("selfpkg", "selfpkg.logic"):
        sys.modules.pop(mod, None)

    roots = [tmp_path]
    try:
        # First pass.
        mods = discovery.discover_modules(roots=roots, exclude=[], generated_dir="__generated__")
        assert "selfpkg.logic" in mods
        discovery.prepare_import_environment(module_names=mods, roots=roots)
        discovery.import_and_collect(mods, kind="magic")
        first = {e.module for e in get_magic_registry().values()}
        assert "selfpkg.logic" in first
        cached = sys.modules["selfpkg.logic"]

        # Second pass: the self module is carved out of eviction, so re-import is a
        # cache no-op that registers nothing; only preservation keeps the specs.
        mods2 = discovery.discover_modules(roots=roots, exclude=[], generated_dir="__generated__")
        discovery.prepare_import_environment(module_names=mods2, roots=roots)
        discovery.import_and_collect(mods2, kind="magic")

        assert sys.modules["selfpkg.logic"] is cached  # carve-out: not re-imported
        second = {e.module for e in get_magic_registry().values()}
        assert "selfpkg.logic" in second  # preservation refilled it
    finally:
        for mod in ("selfpkg", "selfpkg.logic"):
            sys.modules.pop(mod, None)
