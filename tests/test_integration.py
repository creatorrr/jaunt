from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

from jaunt.discovery import discover_modules, import_and_collect
from jaunt.registry import clear_registries, get_magic_registry, get_test_registry
from jaunt.spec_ref import normalize_spec_ref


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_min_project(tmp_path: Path, *, pkg: str) -> None:
    # Minimal jaunt config + realistic src/tests layout.
    _write(
        tmp_path / "jaunt.toml",
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                "source_roots = [\"src\"]",
                "test_roots = [\"tests\"]",
                "generated_dir = \"__generated__\"",
                "",
            ]
        ),
    )

    _write(
        tmp_path / "src" / pkg / "__init__.py",
        "\n".join(
            [
                "import jaunt",
                "",
                "@jaunt.magic()",
                "def greet(name: str) -> str:",
                "    \"\"\"Magic spec stub.\"\"\"",
                "    raise RuntimeError(\"stub\")",
                "",
            ]
        ),
    )

    _write(
        tmp_path / "tests" / "__init__.py",
        "\n".join(
            [
                "import jaunt",
                "",
                "@jaunt.test()",
                "def test_smoke() -> None:",
                "    \"\"\"Test spec stub.\"\"\"",
                "    return None",
                "",
            ]
        ),
    )


def _restore_module(name: str, original: ModuleType | None, *, existed: bool) -> None:
    if existed:
        assert original is not None
        sys.modules[name] = original
    else:
        sys.modules.pop(name, None)


def test_integration_discovery_and_registry_registration(tmp_path: Path) -> None:
    pkg = "jaunt_tmp_pkg"
    _make_min_project(tmp_path, pkg=pkg)

    # Ensure we can revert any prior imports even if the environment already has
    # a `tests` module loaded (unlikely, but possible).
    orig_sys_path = list(sys.path)
    orig_pkg_mod = sys.modules.get(pkg)
    orig_tests_mod = sys.modules.get("tests")
    had_pkg = pkg in sys.modules
    had_tests = "tests" in sys.modules

    # Any modules we import as part of this test should be removed afterwards.
    before_modules = set(sys.modules.keys())

    clear_registries()
    try:
        sys.path.insert(0, str(tmp_path / "src"))
        sys.path.insert(0, str(tmp_path))

        # Magic discovery rooted at src/.
        magic_mods = discover_modules(
            roots=[tmp_path / "src"],
            exclude=[],
            generated_dir="__generated__",
        )
        assert pkg in magic_mods
        import_and_collect([pkg], kind="magic")

        # Test discovery rooted at project root (so tests/__init__.py is included).
        test_mods = discover_modules(
            roots=[tmp_path],
            exclude=["src/**"],
            generated_dir="__generated__",
        )
        assert "tests" in test_mods
        import_and_collect(["tests"], kind="test")

        # Reload the package module to make sure repeated imports are safe.
        pkg_mod = importlib.import_module(pkg)
        importlib.reload(pkg_mod)

        assert normalize_spec_ref(f"{pkg}:greet") in get_magic_registry()
        assert normalize_spec_ref("tests:test_smoke") in get_test_registry()
    finally:
        clear_registries()

        # Restore sys.path first so we don't accidentally re-import tmp modules.
        sys.path[:] = orig_sys_path

        # Remove any new tmp modules imported by this test.
        for name in set(sys.modules.keys()) - before_modules:
            is_tmp_pkg = name == pkg or name.startswith(pkg + ".")
            is_tmp_tests = name == "tests" or name.startswith("tests.")
            if is_tmp_pkg or is_tmp_tests:
                sys.modules.pop(name, None)

        _restore_module(pkg, orig_pkg_mod, existed=had_pkg)
        _restore_module("tests", orig_tests_mod, existed=had_tests)
