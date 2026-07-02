import sys
from pathlib import Path

import jaunt.cli
from jaunt import journal
from jaunt.cli import main
from test_regressions_review_fixes import (
    GoodBackend,
    _restore_modules,
    _write,
    _write_package_init,
)


def _spec_source(docstring: str = "Generate a no-op smoke function.") -> str:
    return "\n".join(
        [
            "from __future__ import annotations",
            "",
            "import jaunt",
            "",
            "@jaunt.magic()",
            "def generated_smoke() -> None:",
            f'    """{docstring}"""',
            '    raise RuntimeError("spec stub")',
            "",
        ]
    )


def _make_build_project(root: Path) -> tuple[Path, str]:
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
    _write(project / "src" / "app" / "specs.py", _spec_source())
    return project, "app"


def test_log_command_prints_tail(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    journal.append_events(
        tmp_path,
        [journal.JournalEvent(action="build", module=f"m{i}", detail="d") for i in range(30)],
        create=True,
    )
    rc = main(["log", "-n", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("\n") == 5
    assert "m29" in out


def test_log_command_module_filter_and_empty(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["log"])
    assert rc == 0
    assert "no journal" in capsys.readouterr().out.lower()


def test_build_appends_journal_when_log_exists(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_build_project(tmp_path)
    (project / journal.JOURNAL_FILE).write_text("", encoding="utf-8")
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    capsys.readouterr()
    assert rc == jaunt.cli.EXIT_OK
    assert any(
        "build" in line and "app.specs" in line and "rebuilt" in line
        for line in journal.read_lines(project, limit=0)
    )


def test_build_does_not_create_journal_without_opt_in(tmp_path: Path, monkeypatch, capsys) -> None:
    project, prefix = _make_build_project(tmp_path)
    before = {
        prefix: sys.modules.get(prefix),
        f"{prefix}.specs": sys.modules.get(f"{prefix}.specs"),
    }
    orig_sys_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())

    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--json"])
    finally:
        sys.path[:] = orig_sys_path
        _restore_modules([prefix], before=before)

    capsys.readouterr()
    assert rc == jaunt.cli.EXIT_OK
    assert not (project / journal.JOURNAL_FILE).exists()
