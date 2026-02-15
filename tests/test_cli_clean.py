"""Tests for `jaunt clean` command."""

from __future__ import annotations

import json
from pathlib import Path

import jaunt.cli


def _make_min_project(root: Path, *, generated_dir: str = "__generated__") -> None:
    """Create a minimal jaunt project structure with generated files."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "jaunt.toml").write_text(
        f'version = 1\n\n[paths]\ngenerated_dir = "{generated_dir}"\n',
        encoding="utf-8",
    )
    # Create generated dirs in source roots
    gen_src = root / "src" / "pkg" / generated_dir
    gen_src.mkdir(parents=True, exist_ok=True)
    (gen_src / "__init__.py").write_text("", encoding="utf-8")
    (gen_src / "specs.py").write_text("# generated\ndef Foo(): pass\n", encoding="utf-8")

    # Create generated dirs in test roots
    gen_test = root / "tests" / generated_dir
    gen_test.mkdir(parents=True, exist_ok=True)
    (gen_test / "__init__.py").write_text("", encoding="utf-8")
    (gen_test / "test_specs.py").write_text("# generated\ndef test_foo(): pass\n", encoding="utf-8")


def test_parse_clean_defaults() -> None:
    ns = jaunt.cli.parse_args(["clean"])
    assert ns.command == "clean"
    assert ns.json_output is False
    assert ns.dry_run is False


def test_parse_clean_flags() -> None:
    ns = jaunt.cli.parse_args(["clean", "--dry-run", "--json"])
    assert ns.dry_run is True
    assert ns.json_output is True


def test_main_dispatches_clean(monkeypatch) -> None:
    monkeypatch.setattr(jaunt.cli, "cmd_clean", lambda args: 0)
    assert jaunt.cli.main(["clean"]) == 0


def test_cmd_clean_removes_generated_dirs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _make_min_project(tmp_path)

    ns = jaunt.cli.parse_args(["clean"])
    rc = jaunt.cli.cmd_clean(ns)
    assert rc == 0

    # __generated__ directories should be gone
    assert not (tmp_path / "src" / "pkg" / "__generated__").exists()
    assert not (tmp_path / "tests" / "__generated__").exists()

    # Non-generated files should remain
    assert (tmp_path / "src").exists()
    assert (tmp_path / "tests").exists()
    assert (tmp_path / "jaunt.toml").exists()


def test_cmd_clean_dry_run_preserves_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _make_min_project(tmp_path)

    ns = jaunt.cli.parse_args(["clean", "--dry-run"])
    rc = jaunt.cli.cmd_clean(ns)
    assert rc == 0

    # Files should still exist
    assert (tmp_path / "src" / "pkg" / "__generated__" / "specs.py").exists()
    assert (tmp_path / "tests" / "__generated__" / "test_specs.py").exists()


def test_cmd_clean_json_output(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _make_min_project(tmp_path)

    ns = jaunt.cli.parse_args(["clean", "--json"])
    rc = jaunt.cli.cmd_clean(ns)
    assert rc == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["command"] == "clean"
    assert data["ok"] is True
    assert len(data["removed"]) == 2


def test_cmd_clean_json_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _make_min_project(tmp_path)

    ns = jaunt.cli.parse_args(["clean", "--dry-run", "--json"])
    rc = jaunt.cli.cmd_clean(ns)
    assert rc == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["command"] == "clean"
    assert data["ok"] is True
    assert data["dry_run"] is True
    assert len(data["would_remove"]) == 2


def test_cmd_clean_no_generated_dirs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")

    ns = jaunt.cli.parse_args(["clean"])
    rc = jaunt.cli.cmd_clean(ns)
    assert rc == 0


def test_cmd_clean_with_root_flag(tmp_path: Path) -> None:
    _make_min_project(tmp_path)

    ns = jaunt.cli.parse_args(["clean", "--root", str(tmp_path)])
    rc = jaunt.cli.cmd_clean(ns)
    assert rc == 0
    assert not (tmp_path / "src" / "pkg" / "__generated__").exists()


def test_cmd_clean_custom_generated_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _make_min_project(tmp_path, generated_dir="__gen__")

    ns = jaunt.cli.parse_args(["clean"])
    rc = jaunt.cli.cmd_clean(ns)
    assert rc == 0
    assert not (tmp_path / "src" / "pkg" / "__gen__").exists()
    assert not (tmp_path / "tests" / "__gen__").exists()


def test_cmd_clean_missing_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # No jaunt.toml
    ns = jaunt.cli.parse_args(["clean"])
    rc = jaunt.cli.cmd_clean(ns)
    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY
