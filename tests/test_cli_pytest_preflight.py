from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import jaunt.cli
from jaunt import tester
from jaunt.errors import JauntConfigError
from jaunt.tester import PytestResult


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_cli_test_project(root: Path) -> Path:
    project = root / "proj"
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
    _write(
        project / "src" / "api_mod.py",
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import jaunt",
                "",
                "@jaunt.magic()",
                "def add_one(value: int) -> int:",
                '    """Return value plus one."""',
                '    raise RuntimeError("spec stub")',
                "",
            ]
        ),
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
                '    """Generated tests should run."""',
                '    raise AssertionError("spec stub")',
                "",
            ]
        ),
    )
    return project


def _restore_modules(before: dict[str, object | None]) -> None:
    for name in list(sys.modules):
        if name == "api_mod" or name == "tests" or name.startswith("tests."):
            sys.modules.pop(name, None)
    for name, module in before.items():
        if module is not None:
            sys.modules[name] = module  # type: ignore[assignment]


def test_pytest_available_returns_true() -> None:
    assert tester.pytest_available() is True


def test_ensure_pytest_available_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str):
        if name == "pytest":
            return None
        return real_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    with pytest.raises(JauntConfigError) as excinfo:
        tester.ensure_pytest_available()

    assert "pytest is now a core dependency" in str(excinfo.value)


def test_cmd_test_preflights_pytest_before_run_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _make_cli_test_project(tmp_path)
    before = {
        "api_mod": sys.modules.get("api_mod"),
        "tests": sys.modules.get("tests"),
        "tests.specs_mod": sys.modules.get("tests.specs_mod"),
    }
    orig_sys_path = list(sys.path)
    real_cost_tracker = jaunt.cli._command_cost_tracker
    events: list[str] = []

    def tracking_cost_tracker(*args, **kwargs):
        events.append("cost-tracker")
        return real_cost_tracker(*args, **kwargs)

    def fail_build_backend(cfg):
        events.append("backend")
        raise AssertionError("_build_backend should not be called before pytest preflight")

    def fail_run_tests(*args, **kwargs):
        raise AssertionError("run_tests should not be called before pytest preflight")

    monkeypatch.setattr(tester, "pytest_available", lambda: False)
    monkeypatch.setattr(jaunt.cli, "_command_cost_tracker", tracking_cost_tracker)
    monkeypatch.setattr(jaunt.cli, "_build_backend", fail_build_backend)
    monkeypatch.setattr(tester, "run_tests", fail_run_tests)

    try:
        args = jaunt.cli.parse_args(["test", "--root", str(project), "--no-build"])
        rc = jaunt.cli.cmd_test(args)
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules(before)

    captured = capsys.readouterr()
    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY
    assert "pytest is not installed" in captured.err
    assert "pytest is now a core dependency" in captured.err
    assert events == ["cost-tracker"]


def test_cmd_test_no_run_skips_pytest_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_cli_test_project(tmp_path)
    before = {
        "api_mod": sys.modules.get("api_mod"),
        "tests": sys.modules.get("tests"),
        "tests.specs_mod": sys.modules.get("tests.specs_mod"),
    }
    orig_sys_path = list(sys.path)

    def fake_run_tests(*args, **kwargs) -> PytestResult:
        return PytestResult(
            exit_code=0,
            passed=True,
            failed=False,
            failures=[],
            generated={"tests.specs_mod"},
        )

    monkeypatch.setattr(tester, "pytest_available", lambda: False)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: object())
    monkeypatch.setattr(tester, "run_tests", fake_run_tests)

    try:
        args = jaunt.cli.parse_args(["test", "--root", str(project), "--no-build", "--no-run"])
        rc = jaunt.cli.cmd_test(args)
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules(before)

    assert rc == jaunt.cli.EXIT_OK
