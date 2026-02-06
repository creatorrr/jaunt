from __future__ import annotations

import sys
from pathlib import Path

import pytest

import jaunt.cli
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class FakeBackend(GeneratorBackend):
    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> str:
        # Generated tests should be able to import modules from paths.source_roots.
        lines: list[str] = ["from dice_demo import VALUE", ""]
        for name in ctx.expected_names:
            lines.extend(
                [
                    f"def {name}() -> None:",
                    "    assert VALUE == 1",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"


def _restore_module(name: str, original, *, existed: bool) -> None:
    if existed:
        assert original is not None
        sys.modules[name] = original
    else:
        sys.modules.pop(name, None)


def test_jaunt_test_sets_pythonpath_for_pytest_subprocess(
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

    # Satisfy config validation (at least one source root must exist).
    _write(project / "src" / "dice_demo" / "__init__.py", "VALUE = 1\n")

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
                "def test_imports_project_module() -> None:",
                '    """Generated tests should import modules from paths.source_roots."""',
                '    raise AssertionError("spec stub")',
                "",
            ]
        ),
    )

    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: FakeBackend())

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
            if name == "tests" or name.startswith("tests."):
                sys.modules.pop(name, None)
        _restore_module("tests", orig_tests_mod, existed=had_tests)
