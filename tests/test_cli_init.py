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


def test_cmd_init_creates_tests_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ns = jaunt.cli.parse_args(["init"])
    rc = jaunt.cli.cmd_init(ns)
    assert rc == 0
    assert (tmp_path / "tests").is_dir()


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


def test_cmd_init_toml_is_valid(tmp_path: Path, monkeypatch) -> None:
    """Generated jaunt.toml should be loadable by the config system."""
    import tomllib

    monkeypatch.chdir(tmp_path)
    ns = jaunt.cli.parse_args(["init"])
    jaunt.cli.cmd_init(ns)

    raw = (tmp_path / "jaunt.toml").read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    assert data["version"] == 1
    assert data["llm"]["provider"] in ("openai", "anthropic")
