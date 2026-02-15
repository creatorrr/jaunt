"""Tests for `jaunt watch` command."""

from __future__ import annotations

import json
from pathlib import Path

import jaunt.cli


def test_parse_watch_defaults() -> None:
    ns = jaunt.cli.parse_args(["watch"])
    assert ns.command == "watch"
    assert ns.json_output is False
    assert ns.root is None
    assert ns.config is None
    assert ns.jobs is None
    assert ns.force is False
    assert ns.test is False
    assert ns.target == []
    assert ns.no_infer_deps is False
    assert ns.no_progress is False


def test_parse_watch_test_flag() -> None:
    ns = jaunt.cli.parse_args(["watch", "--test"])
    assert ns.test is True


def test_parse_watch_json_flag() -> None:
    ns = jaunt.cli.parse_args(["watch", "--json"])
    assert ns.json_output is True


def test_parse_watch_all_flags() -> None:
    ns = jaunt.cli.parse_args(
        [
            "watch",
            "--test",
            "--json",
            "--root",
            "/tmp",
            "--config",
            "/tmp/jaunt.toml",
            "--jobs",
            "2",
            "--force",
            "--target",
            "pkg.mod",
            "--no-infer-deps",
            "--no-progress",
        ]
    )
    assert ns.command == "watch"
    assert ns.test is True
    assert ns.json_output is True
    assert ns.root == "/tmp"
    assert ns.jobs == 2
    assert ns.force is True
    assert ns.target == ["pkg.mod"]
    assert ns.no_infer_deps is True
    assert ns.no_progress is True


def test_main_dispatches_watch(monkeypatch) -> None:
    monkeypatch.setattr(jaunt.cli, "cmd_watch", lambda args: 0)
    assert jaunt.cli.main(["watch"]) == 0


def test_cmd_watch_missing_watchfiles(tmp_path: Path, monkeypatch) -> None:
    """cmd_watch should exit EXIT_CONFIG_OR_DISCOVERY when watchfiles is missing."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()

    import jaunt.watcher

    monkeypatch.setattr(
        jaunt.watcher,
        "check_watchfiles_available",
        _raise_import_error,
    )

    ns = jaunt.cli.parse_args(["watch"])
    rc = jaunt.cli.cmd_watch(ns)
    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY


def test_cmd_watch_missing_watchfiles_json(tmp_path: Path, monkeypatch, capsys) -> None:
    """JSON mode should emit error JSON when watchfiles is missing."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()

    import jaunt.watcher

    monkeypatch.setattr(
        jaunt.watcher,
        "check_watchfiles_available",
        _raise_import_error,
    )

    ns = jaunt.cli.parse_args(["watch", "--json"])
    rc = jaunt.cli.cmd_watch(ns)
    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["command"] == "watch"
    assert data["ok"] is False
    assert "watchfiles" in data["error"]


def test_cmd_watch_missing_config(tmp_path: Path, monkeypatch) -> None:
    """cmd_watch should exit EXIT_CONFIG_OR_DISCOVERY when no jaunt.toml."""
    monkeypatch.chdir(tmp_path)
    # No jaunt.toml

    import jaunt.watcher

    monkeypatch.setattr(jaunt.watcher, "check_watchfiles_available", lambda: None)

    ns = jaunt.cli.parse_args(["watch"])
    rc = jaunt.cli.cmd_watch(ns)
    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY


def _raise_import_error() -> None:
    raise ImportError(
        "watchfiles is required for watch mode. "
        "Install it with: pip install jaunt[watch]"
    )
