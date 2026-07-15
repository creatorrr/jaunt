"""Tests for `jaunt init` command."""

from __future__ import annotations

import json
from pathlib import Path

import jaunt.cli


def test_parse_init_defaults() -> None:
    ns = jaunt.cli.parse_args(["init"])
    assert ns.command == "init"
    assert ns.json_output is False


def test_parse_init_json_flag() -> None:
    ns = jaunt.cli.parse_args(["init", "--json"])
    assert ns.json_output is True


def test_parse_init_force_flag() -> None:
    ns = jaunt.cli.parse_args(["init", "--force"])
    assert ns.force is True


def test_main_dispatches_init(monkeypatch) -> None:
    monkeypatch.setattr(jaunt.cli, "cmd_init", lambda args: 0)
    assert jaunt.cli.main(["init"]) == 0


def test_cmd_init_creates_jaunt_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # init needs at least one source root to exist
    (tmp_path / "src").mkdir()

    ns = jaunt.cli.parse_args(["init"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0
    assert (tmp_path / "jaunt.toml").exists()
    content = (tmp_path / "jaunt.toml").read_text()
    assert "version = 1" in content
    assert "[llm]" in content
    assert "[paths]" in content


def test_cmd_init_refuses_overwrite_without_force(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text("version = 1\n")

    ns = jaunt.cli.parse_args(["init"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY


def test_cmd_init_force_overwrites(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text("old content\n")

    ns = jaunt.cli.parse_args(["init", "--force"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0
    content = (tmp_path / "jaunt.toml").read_text()
    assert "version = 1" in content
    assert "old content" not in content


def test_cmd_init_creates_src_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # No src/ dir exists — init should create it
    ns = jaunt.cli.parse_args(["init"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0
    assert (tmp_path / "src").is_dir()


def test_cmd_init_creates_example_spec(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    ns = jaunt.cli.parse_args(["init"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0

    spec_path = tmp_path / "src" / "specs.py"
    content = spec_path.read_text()
    assert "jaunt.magic_module(__name__)" in content
    assert "greet" in content
    assert "@jaunt.magic" not in content
    assert "raise NotImplementedError" not in content
    assert content.rstrip().endswith("...")


def test_cmd_init_does_not_overwrite_existing_example_spec(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    spec_path = src_dir / "specs.py"
    sentinel = "# sentinel spec\n"
    spec_path.write_text(sentinel)

    ns = jaunt.cli.parse_args(["init"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0
    assert spec_path.read_text() == sentinel

    ns = jaunt.cli.parse_args(["init", "--force"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0
    assert spec_path.read_text() == sentinel


def test_cmd_init_example_spec_is_valid_python(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    ns = jaunt.cli.parse_args(["init"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0

    spec_path = tmp_path / "src" / "specs.py"
    compile(spec_path.read_text(), str(spec_path), "exec")


def test_init_scaffold_classifies_exactly_one_module_spec(tmp_path: Path, monkeypatch) -> None:
    import ast

    from jaunt.module_magic import scan_module_source

    monkeypatch.chdir(tmp_path)
    jaunt.cli.cmd_init(jaunt.cli.parse_args(["init"]))
    src = (tmp_path / "src" / "specs.py").read_text()
    scan = scan_module_source(ast.parse(src), module="specs")
    assert [c.name for c in scan.candidates] == ["greet"]
    assert all(not c.is_class for c in scan.candidates)


def test_cmd_init_creates_tests_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ns = jaunt.cli.parse_args(["init"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0
    assert (tmp_path / "tests").is_dir()


def test_init_scaffolds_journal_and_attributes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ns = jaunt.cli.parse_args(["init"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0
    assert (tmp_path / "JAUNT_LOG").exists()
    assert "JAUNT_LOG merge=union" in (tmp_path / ".gitattributes").read_text(encoding="utf-8")
    assert ".jaunt/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".jaunt-vitest-cache/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


def test_init_journal_scaffolding_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    ns = jaunt.cli.parse_args(["init"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0

    ns = jaunt.cli.parse_args(["init", "--force"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0

    gitattributes_lines = (tmp_path / ".gitattributes").read_text(encoding="utf-8").splitlines()
    gitignore_lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert gitattributes_lines.count("JAUNT_LOG merge=union") == 1
    assert gitignore_lines.count(".jaunt/") == 1
    assert gitignore_lines.count(".jaunt-vitest-cache/") == 1


def test_cmd_init_json_output(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()

    ns = jaunt.cli.parse_args(["init", "--json"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["command"] == "init"
    assert data["ok"] is True
    assert "path" in data
    assert data["spec_path"] == str(tmp_path / "src" / "specs.py")


def test_cmd_init_json_omits_spec_path_when_example_spec_exists(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "specs.py").write_text("# sentinel spec\n")

    ns = jaunt.cli.parse_args(["init", "--json"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["command"] == "init"
    assert data["ok"] is True
    assert "path" in data
    assert "spec_path" not in data


def test_cmd_init_json_output_on_existing(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text("version = 1\n")

    ns = jaunt.cli.parse_args(["init", "--json"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["command"] == "init"
    assert data["ok"] is False
    assert "error" in data


def test_cmd_init_with_root_flag(tmp_path: Path) -> None:
    target = tmp_path / "myproject"
    target.mkdir()
    (target / "src").mkdir()

    ns = jaunt.cli.parse_args(["init", "--root", str(target)])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0
    assert (target / "jaunt.toml").exists()


def test_cmd_init_template_includes_codex_setup_hint(tmp_path: Path, monkeypatch) -> None:
    """Generated jaunt.toml should guide the user toward Codex engine setup."""
    monkeypatch.chdir(tmp_path)
    ns = jaunt.cli.parse_args(["init"])
    jaunt.cli.cmd_init(ns)

    content = (tmp_path / "jaunt.toml").read_text()
    assert "[codex]" in content
    assert "codex login" in content


def test_cmd_init_template_includes_all_supported_sections(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ns = jaunt.cli.parse_args(["init"])
    jaunt.cli.cmd_init(ns)

    content = (tmp_path / "jaunt.toml").read_text()
    assert "[build]" in content
    assert "include_target_tests = false" in content
    assert "check_generated_imports = true" in content
    assert "generated_import_allowlist" in content
    assert "instructions =" in content
    assert "[test]" in content
    assert "[prompts]" in content


def test_cmd_init_toml_is_valid(tmp_path: Path, monkeypatch) -> None:
    """Generated jaunt.toml should be loadable by the config system."""
    import tomllib

    monkeypatch.chdir(tmp_path)
    ns = jaunt.cli.parse_args(["init"])
    jaunt.cli.cmd_init(ns)

    raw = (tmp_path / "jaunt.toml").read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    assert data["version"] == 1
    assert data["llm"]["provider"] in ("openai", "anthropic", "cerebras")
