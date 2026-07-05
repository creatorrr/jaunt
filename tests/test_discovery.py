from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from jaunt.discovery import (
    _has_jaunt_markers,
    discover_modules,
    evict_modules_for_import,
    import_and_collect,
)
from jaunt.errors import JauntDiscoveryError


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_discover_modules_finds_pkg_modules(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "foo.py", "X = 1\n")
    _write(tmp_path / "pkg" / "bar.py", "Y = 2\n")

    mods = discover_modules(
        roots=[tmp_path],
        exclude=[],
        generated_dir="__generated__",
        spec_prescreen=False,  # markerless fixtures: path→name, not spec filtering
    )

    assert "pkg.foo" in mods
    assert "pkg.bar" in mods
    assert mods == sorted(mods)


def test_discover_modules_excludes_generated_dir(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "__generated__" / "gen.py", "Z = 3\n")
    _write(tmp_path / "pkg" / "ok.py", "OK = True\n")

    mods = discover_modules(
        roots=[tmp_path],
        exclude=[],
        generated_dir="__generated__",
        spec_prescreen=False,  # markerless fixtures: path→name, not spec filtering
    )

    assert "pkg.__generated__.gen" not in mods
    assert "pkg.ok" in mods


def test_discover_modules_honors_exclude_globs(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "ok.py", "OK = True\n")
    _write(tmp_path / ".venv" / "site.py", "NOPE = 1\n")

    mods = discover_modules(
        roots=[tmp_path],
        exclude=["**/.venv/**"],
        generated_dir="__generated__",
        spec_prescreen=False,  # markerless fixtures: path→name, not spec filtering
    )

    assert "pkg.ok" in mods
    assert ".venv.site" not in mods


def test_discover_modules_with_module_prefix(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "__init__.py", "")
    _write(tmp_path / "tests" / "specs_mod.py", "X = 1\n")

    mods = discover_modules(
        roots=[tmp_path / "tests"],
        exclude=[],
        generated_dir="__generated__",
        module_prefix="tests",
        spec_prescreen=False,  # markerless fixtures: path→name, not spec filtering
    )

    assert "tests" in mods
    assert "tests.specs_mod" in mods
    assert "specs_mod" not in mods


def test_import_and_collect_for_prefixed_tests_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "tests" / "__init__.py", "")
    _write(tmp_path / "tests" / "specs_mod.py", "VALUE = 123\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    orig_tests = sys.modules.get("tests")
    orig_sub = sys.modules.get("tests.specs_mod")
    had_tests = "tests" in sys.modules
    had_sub = "tests.specs_mod" in sys.modules
    try:
        sys.modules.pop("tests.specs_mod", None)
        sys.modules.pop("tests", None)
        import_and_collect(["tests.specs_mod"], kind="test")
    finally:
        sys.modules.pop("tests.specs_mod", None)
        sys.modules.pop("tests", None)
        if had_sub:
            assert orig_sub is not None
            sys.modules["tests.specs_mod"] = orig_sub
        if had_tests:
            assert orig_tests is not None
            sys.modules["tests"] = orig_tests


def test_import_and_collect_wraps_import_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "badmod.py", "def oops(:\n    pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(JauntDiscoveryError) as excinfo:
        import_and_collect(["badmod"], kind="test")

    assert "badmod" in str(excinfo.value)


def test_discover_modules_with_target_modules_skips_scan(tmp_path: Path) -> None:
    """When target_modules is provided, only those modules should be returned."""
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "foo.py", "X = 1\n")
    _write(tmp_path / "pkg" / "bar.py", "Y = 2\n")
    _write(tmp_path / "pkg" / "baz.py", "Z = 3\n")

    mods = discover_modules(
        roots=[tmp_path],
        exclude=[],
        generated_dir="__generated__",
        target_modules={"pkg.foo", "pkg.bar"},
    )

    # Only the targeted modules should be returned, not all discovered modules.
    assert "pkg.foo" in mods
    assert "pkg.bar" in mods
    assert "pkg.baz" not in mods


def test_prescreen_skips_markerless_module(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "boobytrap.py").write_text("raise RuntimeError('imported!')\n")
    (root / "spec_mod.py").write_text("import jaunt\n@jaunt.magic()\ndef f() -> int:\n    ...\n")
    names = discover_modules(roots=[root], exclude=[], generated_dir="__generated__")
    assert names == ["spec_mod"]


def test_prescreen_passes_bare_decorator_form(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "bare_mod.py").write_text(
        "from jaunt import magic\n@magic()\ndef f() -> int:\n    ...\n"
    )
    names = discover_modules(roots=[root], exclude=[], generated_dir="__generated__")
    assert "bare_mod" in names


def test_prescreen_recognizes_magic_module_call() -> None:
    src = "import jaunt\njaunt.magic_module(__name__)\n"
    assert _has_jaunt_markers(src)


def test_prescreen_recognizes_bare_magic_module_call_without_import() -> None:
    # belt-and-braces branch: call form alone, no importable jaunt alias
    src = "from spam import magic_module\nmagic_module(__name__)\n"
    assert _has_jaunt_markers(src)


def test_prescreen_skips_syntax_error_file_quietly(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    # Contains the substring 'jaunt' so it passes the textual prefilter, but does
    # not parse — must be skipped silently (no raise).
    (root / "broken.py").write_text("import jaunt\ndef oops(:\n    pass\n")
    names = discover_modules(roots=[root], exclude=[], generated_dir="__generated__")
    assert names == []


def test_target_fast_path_bypasses_prescreen(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "boobytrap.py").write_text("raise RuntimeError('imported!')\n")
    names = discover_modules(
        roots=[root],
        exclude=[],
        generated_dir="__generated__",
        target_modules={"boobytrap"},
    )
    assert names == ["boobytrap"]


def test_textual_prefilter_short_circuits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "plain.py").write_text("X = 1\n")  # no 'jaunt' substring
    import jaunt.discovery as discovery_mod

    called = False
    real_parse = ast.parse

    def _spy_parse(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal called
        called = True
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(discovery_mod.ast, "parse", _spy_parse)
    names = discover_modules(roots=[root], exclude=[], generated_dir="__generated__")
    assert names == []
    assert called is False


def test_import_and_collect_imports_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "okmod.py", "VALUE = 123\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    import_and_collect(["okmod"], kind="test")


def test_evict_modules_for_import_drops_parent_packages_of_target_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first / "tests" / "__init__.py", "")
    _write(first / "tests" / "specs_mod.py", "VALUE = 'first'\n")
    _write(second / "tests" / "__init__.py", "")
    _write(second / "tests" / "specs_mod.py", "VALUE = 'second'\n")

    monkeypatch.syspath_prepend(str(first))
    orig_tests = sys.modules.get("tests")
    orig_specs = sys.modules.get("tests.specs_mod")
    had_tests = "tests" in sys.modules
    had_specs = "tests.specs_mod" in sys.modules
    try:
        sys.modules.pop("tests.specs_mod", None)
        sys.modules.pop("tests", None)
        import_and_collect(["tests.specs_mod"], kind="test")
        assert sys.modules["tests.specs_mod"].VALUE == "first"

        monkeypatch.syspath_prepend(str(second))
        evict_modules_for_import(module_names=["tests.specs_mod"], roots=[second / "tests"])
        import_and_collect(["tests.specs_mod"], kind="test")

        assert sys.modules["tests.specs_mod"].VALUE == "second"
    finally:
        sys.modules.pop("tests.specs_mod", None)
        sys.modules.pop("tests", None)
        if had_specs:
            assert orig_specs is not None
            sys.modules["tests.specs_mod"] = orig_specs
        if had_tests:
            assert orig_tests is not None
            sys.modules["tests"] = orig_tests


def test_evict_modules_for_import_never_evicts_running_jaunt_package(tmp_path: Path) -> None:
    """Self-hosting bug 1: the running framework is carved out of eviction even
    when its source dir is a configured root."""
    import types

    import jaunt
    import jaunt.discovery as jd  # noqa: F401

    src_root = Path(jaunt.__file__).resolve().parent

    planted = types.ModuleType("planted_under_jaunt_root")
    planted.__file__ = str(src_root / "planted_under_jaunt_root.py")
    sys.modules["planted_under_jaunt_root"] = planted

    saved = {k: v for k, v in sys.modules.items() if k == "jaunt" or k.startswith("jaunt.")}
    try:
        evict_modules_for_import(module_names=["jaunt"], roots=[src_root])
        assert "jaunt" in sys.modules
        assert "jaunt.discovery" in sys.modules
        assert "planted_under_jaunt_root" not in sys.modules
    finally:
        for k, v in saved.items():
            sys.modules.setdefault(k, v)
        sys.modules.pop("planted_under_jaunt_root", None)


# ---------------------------------------------------------------------------
# 1.3.0 layout/naming warnings (findings 6/9/12) — Task 5
# ---------------------------------------------------------------------------


def test_shadow_warning_for_stdlib_top_level_name(tmp_path: Path) -> None:
    from jaunt.discovery import reset_discovery_warnings

    reset_discovery_warnings()
    _write(tmp_path / "json.py", "X = 1\n")  # 'json' is in sys.stdlib_module_names
    with pytest.warns(UserWarning, match="shadow"):
        mods = discover_modules(
            roots=[tmp_path],
            exclude=[],
            generated_dir="__generated__",
            spec_prescreen=False,
        )
    assert "json" in mods


def test_no_shadow_warning_for_package_member(
    tmp_path: Path, recwarn: pytest.WarningsRecorder
) -> None:
    from jaunt.discovery import reset_discovery_warnings

    reset_discovery_warnings()
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "json.py", "X = 1\n")  # pkg.json is not a top-level name
    discover_modules(
        roots=[tmp_path],
        exclude=[],
        generated_dir="__generated__",
        spec_prescreen=False,
    )
    assert not any("shadow" in str(w.message) for w in recwarn.list)


def test_shadow_warning_emitted_once_per_run(
    tmp_path: Path, recwarn: pytest.WarningsRecorder
) -> None:
    from jaunt.discovery import reset_discovery_warnings

    reset_discovery_warnings()
    _write(tmp_path / "json.py", "X = 1\n")
    discover_modules(
        roots=[tmp_path], exclude=[], generated_dir="__generated__", spec_prescreen=False
    )
    discover_modules(
        roots=[tmp_path], exclude=[], generated_dir="__generated__", spec_prescreen=False
    )
    shadow_warnings = [w for w in recwarn.list if "shadow" in str(w.message)]
    assert len(shadow_warnings) == 1


def test_package_root_doctor_warning(tmp_path: Path) -> None:
    from jaunt.discovery import reset_discovery_warnings

    reset_discovery_warnings()
    _write(tmp_path / "__init__.py", "")  # the source root itself is a package
    _write(tmp_path / "mod.py", "X = 1\n")
    with pytest.warns(UserWarning, match="package directory"):
        discover_modules(
            roots=[tmp_path],
            exclude=[],
            generated_dir="__generated__",
            spec_prescreen=False,
        )


def test_no_package_root_warning_for_package_parent(
    tmp_path: Path, recwarn: pytest.WarningsRecorder
) -> None:
    from jaunt.discovery import reset_discovery_warnings

    reset_discovery_warnings()
    # Root is the package *parent* (no __init__.py at the root); the correct layout.
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "mod.py", "X = 1\n")
    discover_modules(
        roots=[tmp_path],
        exclude=[],
        generated_dir="__generated__",
        spec_prescreen=False,
    )
    assert not any("package directory" in str(w.message) for w in recwarn.list)


def test_no_layout_warnings_for_prefixed_test_discovery(
    tmp_path: Path, recwarn: pytest.WarningsRecorder
) -> None:
    from jaunt.discovery import reset_discovery_warnings

    reset_discovery_warnings()
    # Prefixed (test-root) discovery must not emit source-layout warnings even
    # when the root is a package and a module name would shadow the stdlib.
    _write(tmp_path / "__init__.py", "")
    _write(tmp_path / "json.py", "X = 1\n")
    discover_modules(
        roots=[tmp_path],
        exclude=[],
        generated_dir="__generated__",
        module_prefix="tests",
        spec_prescreen=False,
    )
    assert not recwarn.list
