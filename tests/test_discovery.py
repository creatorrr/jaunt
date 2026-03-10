from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jaunt.discovery import discover_modules, evict_modules_for_import, import_and_collect
from jaunt.errors import JauntDiscoveryError


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_discover_modules_finds_pkg_modules(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "foo.py", "X = 1\n")
    _write(tmp_path / "pkg" / "bar.py", "Y = 2\n")

    mods = discover_modules(roots=[tmp_path], exclude=[], generated_dir="__generated__")

    assert "pkg.foo" in mods
    assert "pkg.bar" in mods
    assert mods == sorted(mods)


def test_discover_modules_excludes_generated_dir(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "__generated__" / "gen.py", "Z = 3\n")
    _write(tmp_path / "pkg" / "ok.py", "OK = True\n")

    mods = discover_modules(roots=[tmp_path], exclude=[], generated_dir="__generated__")

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
