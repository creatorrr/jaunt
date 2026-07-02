"""Tests for `jaunt specs` — spec listing + dependency graph (migrated from the MCP server)."""

from __future__ import annotations

import json
from pathlib import Path

import jaunt.cli


def _make_min_project(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "jaunt.toml").write_text(
        'version = 1\n\n[paths]\nsource_roots = ["src"]\ngenerated_dir = "__generated__"\n',
        encoding="utf-8",
    )


def _make_project_with_specs(root: Path) -> None:
    _make_min_project(root)
    (root / "src" / "widgets.py").write_text(
        "import jaunt\n"
        "\n"
        "@jaunt.magic\n"
        "def make_widget(name: str) -> str:\n"
        '    """Return a widget label for *name*."""\n'
        "    ...\n"
        "\n"
        "@jaunt.magic(deps=[make_widget])\n"
        "def make_gadget(name: str) -> str:\n"
        '    """Return a gadget label built from make_widget."""\n'
        "    ...\n",
        encoding="utf-8",
    )


def _run_json(capsys, argv: list[str]) -> dict:
    exit_code = jaunt.cli.main(argv)
    out = capsys.readouterr().out
    data = json.loads(out)
    data["_exit_code"] = exit_code
    return data


class TestSpecsParsing:
    def test_parse_specs(self) -> None:
        ns = jaunt.cli.parse_args(["specs"])
        assert ns.command == "specs"
        assert ns.module is None

    def test_parse_specs_flags(self) -> None:
        ns = jaunt.cli.parse_args(["specs", "--root", "/tmp/proj", "--module", "widgets", "--json"])
        assert ns.root == "/tmp/proj"
        assert ns.module == "widgets"
        assert ns.json_output is True


class TestSpecsCommand:
    def test_no_specs(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.chdir(tmp_path)
        _make_min_project(tmp_path)

        data = _run_json(capsys, ["specs", "--root", str(tmp_path), "--json"])
        assert data["command"] == "specs"
        assert data["ok"] is True
        assert data["specs"] == []
        assert data["dependency_graph"] == {}
        assert data["_exit_code"] == 0

    def test_missing_config(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.chdir(tmp_path)

        data = _run_json(capsys, ["specs", "--root", str(tmp_path), "--json"])
        assert data["command"] == "specs"
        assert data["ok"] is False
        assert "error" in data
        assert data["_exit_code"] == 2

    def test_lists_specs_and_deps(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project_with_specs(tmp_path)

        data = _run_json(capsys, ["specs", "--root", str(tmp_path), "--json"])
        assert data["ok"] is True
        refs = {s["ref"] for s in data["specs"]}
        assert refs == {"widgets:make_widget", "widgets:make_gadget"}
        assert "widgets:make_widget" in data["dependency_graph"]["widgets:make_gadget"]

    def test_module_filter(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project_with_specs(tmp_path)

        data = _run_json(
            capsys, ["specs", "--root", str(tmp_path), "--module", "nonexistent", "--json"]
        )
        assert data["ok"] is True
        assert data["specs"] == []
        assert data["dependency_graph"] == {}

    def test_human_output(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.chdir(tmp_path)
        _make_project_with_specs(tmp_path)

        exit_code = jaunt.cli.main(["specs", "--root", str(tmp_path)])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "specs: 2" in out
        assert "widgets:make_gadget" in out

    def test_sys_path_restored_prefix_only(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """`specs` may prepend source roots to sys.path but must not drop entries."""
        import sys

        monkeypatch.chdir(tmp_path)
        _make_min_project(tmp_path)

        original = sys.path[:]
        jaunt.cli.main(["specs", "--root", str(tmp_path), "--json"])
        capsys.readouterr()
        assert sys.path[-len(original) :] == original
