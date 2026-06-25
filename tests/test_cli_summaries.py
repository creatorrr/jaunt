from __future__ import annotations

import json
import sys
from pathlib import Path

import jaunt.cli
from test_regressions_review_fixes import (
    GoodBackend,
    _make_cli_test_project,
    _restore_modules,
    _write,
    _write_package_init,
)


def _make_cli_build_project(root: Path) -> tuple[Path, str]:
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
            ]
        ),
    )
    _write_package_init(project, "src/app")
    _write(
        project / "src" / "app" / "specs.py",
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import jaunt",
                "",
                "@jaunt.magic()",
                "def generated_smoke() -> None:",
                '    """Generate a no-op smoke function."""',
                '    raise RuntimeError("spec stub")',
                "",
            ]
        ),
    )
    return project, "app"


def test_cli_test_non_json_prints_generation_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_test_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs_mod": sys.modules.get(f"{prefix}.specs_mod"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["test", "--root", str(project), "--no-build", "--no-run"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    out = capsys.readouterr().out
    assert rc == jaunt.cli.EXIT_OK
    assert "Generated 1 test module(s), skipped 0." in out
    assert "test module(s), skipped" in out


def test_cli_test_json_does_not_print_generation_summary(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project, prefix = _make_cli_test_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs_mod": sys.modules.get(f"{prefix}.specs_mod"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["test", "--root", str(project), "--no-build", "--no-run", "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == jaunt.cli.EXIT_OK
    assert payload == {
        "command": "test",
        "ok": True,
        "exit_code": 0,
        "generation_failed": {},
    }
    assert "Generated" not in out
    assert "module(s), skipped" not in out


def test_cli_build_non_json_prints_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_cli_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project)])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    out = capsys.readouterr().out
    assert rc == jaunt.cli.EXIT_OK
    assert "Built " in out
    assert "module(s), skipped" in out
