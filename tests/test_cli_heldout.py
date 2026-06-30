from __future__ import annotations

import sys
from pathlib import Path

import pytest

import jaunt.cli
from jaunt import tester
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


def test_no_redact_derived_flag_parsed() -> None:
    args = jaunt.cli.parse_args(["test", "--no-redact-derived"])
    assert args.no_redact_derived is True
    assert jaunt.cli.parse_args(["test"]).no_redact_derived is False


def test_cmd_test_plumbs_no_redact_derived_and_warns(
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
    captured_kwargs: dict[str, object] = {}

    def fake_run_tests(*args: object, **kwargs: object) -> PytestResult:
        del args
        captured_kwargs.update(kwargs)
        return PytestResult(
            exit_code=0,
            passed=True,
            failed=False,
            failures=[],
            generated={"tests.specs_mod"},
        )

    monkeypatch.setattr(tester, "pytest_available", lambda: True)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: object())
    monkeypatch.setattr(tester, "run_tests", fake_run_tests)

    try:
        args = jaunt.cli.parse_args(
            ["test", "--root", str(project), "--no-build", "--no-redact-derived"]
        )
        rc = jaunt.cli.cmd_test(args)
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules(before)

    stderr = capsys.readouterr().err
    assert rc == jaunt.cli.EXIT_OK
    assert captured_kwargs["no_redact_derived"] is True
    assert "--no-redact-derived" in stderr
    assert "held-out" in stderr
