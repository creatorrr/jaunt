from __future__ import annotations

import json
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
    ) -> tuple[str, None]:
        # Generate a minimal pytest module that defines all expected test functions.
        lines: list[str] = []
        for name in ctx.expected_names:
            lines.append(f"def {name}() -> None:\n    assert True\n")
        return "\n".join(lines).rstrip() + "\n", None


def _restore_module(name: str, original, *, existed: bool) -> None:
    if existed:
        assert original is not None
        sys.modules[name] = original
    else:
        sys.modules.pop(name, None)


def test_jaunt_test_discovers_tests_package_when_test_roots_is_tests(
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
    (project / "src").mkdir(parents=True, exist_ok=True)

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
                '    """Generated tests should run via `jaunt test`."""',
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


def test_jaunt_status_discovers_targeted_tests_without_importing_test_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
            ]
        ),
    )

    _write(project / "src" / "demo" / "__init__.py", "")
    _write(
        project / "src" / "demo" / "core_specs.py",
        "\n".join(
            [
                "import jaunt",
                "",
                "@jaunt.magic()",
                "def parse_move(raw: str) -> int:",
                '    """Parse a move."""',
                '    raise RuntimeError("stub")',
                "",
            ]
        ),
    )
    _write(
        project / "tests" / "core_specs.py",
        "\n".join(
            [
                "import missing_test_only_package",
                "import jaunt",
                "from demo.core_specs import parse_move",
                "",
                "@jaunt.test(targets=[parse_move])",
                "def test_parse_move() -> None:",
                '    """Verify move parsing."""',
                '    raise AssertionError("spec stub")',
                "",
            ]
        ),
    )

    monkeypatch.chdir(project)

    orig_sys_path = list(sys.path)
    before_modules = set(sys.modules.keys())
    try:
        rc = jaunt.cli.main(["status", "--root", str(project), "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["command"] == "status"
        assert "demo.core_specs" in data["stale"]
    finally:
        sys.path[:] = orig_sys_path
        for name in set(sys.modules.keys()) - before_modules:
            if (
                name == "demo"
                or name.startswith("demo.")
                or name == "tests"
                or name.startswith("tests.")
            ):
                sys.modules.pop(name, None)


def test_jaunt_status_does_not_let_test_helpers_shadow_stdlib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
            ]
        ),
    )

    _write(project / "src" / "demo" / "__init__.py", "")
    _write(
        project / "src" / "demo" / "core_specs.py",
        "\n".join(
            [
                "from fractions import Fraction",
                "import jaunt",
                "",
                "@jaunt.magic()",
                "def parse_move(raw: str) -> int:",
                '    """Parse a move."""',
                '    raise RuntimeError("stub")',
                "",
            ]
        ),
    )
    _write(project / "tests" / "fractions.py", "VALUE = 1\n")

    monkeypatch.chdir(project)

    orig_sys_path = list(sys.path)
    before_modules = set(sys.modules.keys())
    try:
        rc = jaunt.cli.main(["status", "--root", str(project), "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["command"] == "status"
        assert "demo.core_specs" in data["stale"]
    finally:
        sys.path[:] = orig_sys_path
        for name in set(sys.modules.keys()) - before_modules:
            if (
                name == "demo"
                or name.startswith("demo.")
                or name == "fractions"
                or name == "tests"
                or name.startswith("tests.")
            ):
                sys.modules.pop(name, None)


def test_jaunt_test_no_build_does_not_let_test_helpers_shadow_stdlib(
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

    _write(project / "src" / "demo" / "__init__.py", "")
    _write(
        project / "src" / "demo" / "core_specs.py",
        "\n".join(
            [
                "from fractions import Fraction",
                "import jaunt",
                "",
                "@jaunt.magic()",
                "def parse_move(raw: str) -> int:",
                '    """Parse a move."""',
                '    raise RuntimeError("stub")',
                "",
            ]
        ),
    )
    _write(project / "tests" / "fractions.py", "VALUE = 1\n")
    _write(
        project / "tests" / "specs_mod.py",
        "\n".join(
            [
                "import jaunt",
                "",
                "@jaunt.test()",
                "def test_smoke() -> None:",
                '    """Generated tests should run."""',
                '    raise AssertionError("spec stub")',
                "",
            ]
        ),
    )

    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: FakeBackend())

    orig_sys_path = list(sys.path)
    before_modules = set(sys.modules.keys())
    try:
        rc = jaunt.cli.main(
            [
                "test",
                "--root",
                str(project),
                "--no-build",
                "--no-run",
            ]
        )
        assert rc == 0
    finally:
        sys.path[:] = orig_sys_path
        for name in set(sys.modules.keys()) - before_modules:
            if (
                name == "demo"
                or name.startswith("demo.")
                or name == "fractions"
                or name == "tests"
                or name.startswith("tests.")
            ):
                sys.modules.pop(name, None)
