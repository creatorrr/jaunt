"""Tests for `jaunt watch` command."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
from pathlib import Path

import jaunt.cli
from jaunt.watcher import WatchCycleResult, WatchEvent


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
    assert ns.progress == "auto"
    assert ns.instructions == []
    assert ns.include_target_tests is None


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
            "--progress",
            "none",
            "--instruction",
            "Prefer narrow imports.",
            "--include-target-tests",
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
    assert ns.progress == "none"
    assert ns.instructions == ["Prefer narrow imports."]
    assert ns.include_target_tests is True


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


def test_cmd_watch_runs_async_cycle_runner_once(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()

    import jaunt.watcher

    monkeypatch.setattr(jaunt.watcher, "check_watchfiles_available", lambda: None)

    async def fake_changes(
        watch_paths: list[Path],
    ) -> AsyncIterator[set[tuple[int, str]]]:
        assert tmp_path / "src" in watch_paths
        yield {(1, str(tmp_path / "src" / "specs.py"))}

    calls: list[WatchEvent] = []

    def fake_build_cycle_runner(args, *, run_tests: bool):
        async def runner(event: WatchEvent) -> WatchCycleResult:
            calls.append(event)
            return WatchCycleResult(
                build_exit_code=0,
                test_exit_code=None,
                duration_s=0.01,
                changed_paths=event.changed_paths,
            )

        return runner

    monkeypatch.setattr(jaunt.watcher, "make_watchfiles_iter", fake_changes)
    monkeypatch.setattr(jaunt.watcher, "build_cycle_runner", fake_build_cycle_runner)

    ns = jaunt.cli.parse_args(["watch"])
    rc = jaunt.cli.cmd_watch(ns)

    assert rc == jaunt.cli.EXIT_OK
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert "[watch] done" in captured.err
    assert "running event loop" not in captured.err


def test_cmd_watch_typescript_scope_includes_workspace_inputs_and_excludes_custom_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "src" / "tokens" / "machine").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        """\
version = 2

[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
generated_dir = "machine"
""",
        encoding="utf-8",
    )

    import jaunt.watcher

    monkeypatch.setattr(jaunt.watcher, "check_watchfiles_available", lambda: None)

    async def fake_changes(
        watch_paths: list[Path],
    ) -> AsyncIterator[set[tuple[int, str]]]:
        assert tmp_path in watch_paths
        assert tmp_path / "src" in watch_paths
        yield {
            (1, str(tmp_path / "src" / "tokens" / "machine" / "index.ts")),
            (1, str(tmp_path / "package-lock.json")),
        }

    calls: list[WatchEvent] = []

    def fake_build_cycle_runner(args, *, run_tests: bool):
        async def runner(event: WatchEvent) -> WatchCycleResult:
            calls.append(event)
            return WatchCycleResult(0, None, 0.01, event.changed_paths)

        return runner

    monkeypatch.setattr(jaunt.watcher, "make_watchfiles_iter", fake_changes)
    monkeypatch.setattr(jaunt.watcher, "build_cycle_runner", fake_build_cycle_runner)

    ns = jaunt.cli.parse_args(["watch", "--root", str(tmp_path)])
    assert jaunt.cli.cmd_watch(ns) == jaunt.cli.EXIT_OK

    assert len(calls) == 1
    assert calls[0].changed_paths == frozenset({tmp_path / "package-lock.json"})


def test_cmd_watch_refreshes_typescript_roots_after_config_edit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src2").mkdir()
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    config_path = tmp_path / "jaunt.toml"

    def write_config(source_root: str) -> None:
        config_path.write_text(
            f"""\
version = 2

[target.ts]
source_roots = ["{source_root}"]
test_roots = []
projects = ["tsconfig.json"]
""",
            encoding="utf-8",
        )

    write_config("src")

    import jaunt.watcher

    monkeypatch.setattr(jaunt.watcher, "check_watchfiles_available", lambda: None)

    new_spec = tmp_path / "src2" / "fresh.jaunt.ts"

    async def fake_changes(
        watch_paths: list[Path],
    ) -> AsyncIterator[set[tuple[int, str]]]:
        # The root-level watch keeps future configured roots observable even
        # though src2 was not one of the roots passed to watchfiles at startup.
        assert tmp_path in watch_paths
        assert tmp_path / "src2" not in watch_paths
        write_config("src2")
        yield {(1, str(config_path))}
        new_spec.write_text("export declare function fresh(): string;\n", encoding="utf-8")
        yield {(1, str(new_spec))}

    calls: list[WatchEvent] = []

    def fake_build_cycle_runner(args, *, run_tests: bool):
        async def runner(event: WatchEvent) -> WatchCycleResult:
            calls.append(event)
            return WatchCycleResult(0, None, 0.01, event.changed_paths)

        return runner

    monkeypatch.setattr(jaunt.watcher, "make_watchfiles_iter", fake_changes)
    monkeypatch.setattr(jaunt.watcher, "build_cycle_runner", fake_build_cycle_runner)

    ns = jaunt.cli.parse_args(["watch", "--root", str(tmp_path)])
    assert jaunt.cli.cmd_watch(ns) == jaunt.cli.EXIT_OK

    assert [event.changed_paths for event in calls] == [
        frozenset({config_path}),
        frozenset({new_spec}),
    ]


def _raise_import_error() -> None:
    raise ImportError(
        "watchfiles is required for watch mode but is not available. "
        "Reinstall jaunt; watchfiles is now a core dependency."
    )
