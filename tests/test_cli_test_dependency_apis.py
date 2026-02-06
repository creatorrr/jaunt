from __future__ import annotations

import sys
from pathlib import Path

import pytest

import jaunt.cli
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class AssertingBackend(GeneratorBackend):
    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> str:
        # The CLI should provide magic specs as Dependency APIs so test generation
        # can import real APIs (not guess module names).
        assert ctx.dependency_apis
        assert any(str(ref).startswith("api_mod:") for ref in ctx.dependency_apis)

        # Minimal passing pytest module.
        lines: list[str] = []
        for name in ctx.expected_names:
            lines.append(f"def {name}() -> None:\n    assert True\n")
        return "\n".join(lines).rstrip() + "\n"


def _restore_module(name: str, original, *, existed: bool) -> None:
    if existed:
        assert original is not None
        sys.modules[name] = original
    else:
        sys.modules.pop(name, None)


def test_jaunt_test_passes_magic_dependency_apis_to_test_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)

    _write(
        project / "jaunt.toml",
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                'source_roots = ["src"]',
                'test_roots = ["tests"]',
                'generated_dir = "__generated__"',
                "",
                "[test]",
                'pytest_args = ["-q"]',
                "",
            ]
        ),
    )

    # src/api_mod.py includes a magic spec that should be discovered and threaded
    # into test generation as dependency_apis.
    _write(
        project / "src" / "api_mod.py",
        "import jaunt\n\n@jaunt.magic()\ndef foo(x: int) -> int:\n    raise RuntimeError('stub')\n",
    )

    _write(project / "tests" / "__init__.py", "")
    _write(
        project / "tests" / "specs_mod.py",
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import jaunt",
                "",
                "@jaunt.test()",
                "def test_generated_smoke() -> None:",
                "    raise AssertionError('spec stub')",
                "",
            ]
        ),
    )

    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: AssertingBackend())

    orig_sys_path = list(sys.path)
    orig_tests_mod = sys.modules.get("tests")
    had_tests = "tests" in sys.modules
    before_modules = set(sys.modules.keys())
    try:
        rc = jaunt.cli.main(
            [
                "test",
                "--root",
                str(project),
                "--no-build",
                "--pytest-args=-q",
            ]
        )
        assert rc == 0
    finally:
        sys.path[:] = orig_sys_path
        for name in set(sys.modules.keys()) - before_modules:
            if name in ("tests", "api_mod") or name.startswith("tests."):
                sys.modules.pop(name, None)
        _restore_module("tests", orig_tests_mod, existed=had_tests)
