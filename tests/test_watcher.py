"""Tests for jaunt.watcher module."""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from jaunt.watcher import (
    WatchCycleResult,
    WatchEvent,
    build_cycle_runner,
    filter_spec_files,
    format_watch_cycle_json,
    run_watch_loop,
)
from jaunt.watcher import _python_selected, _typescript_targets

# ---------------------------------------------------------------------------
# Round 2: Optional dependency check
# ---------------------------------------------------------------------------


def test_check_watchfiles_available_raises_when_missing(monkeypatch) -> None:
    """Should raise ImportError with a helpful install message."""
    # Ensure watchfiles is not importable
    monkeypatch.setitem(sys.modules, "watchfiles", None)

    from jaunt.watcher import check_watchfiles_available

    with pytest.raises(ImportError, match="core dependency"):
        check_watchfiles_available()


def test_check_watchfiles_available_succeeds_when_installed(monkeypatch) -> None:
    """Should not raise when watchfiles is importable."""
    fake = types.ModuleType("watchfiles")
    monkeypatch.setitem(sys.modules, "watchfiles", fake)

    from jaunt.watcher import check_watchfiles_available

    check_watchfiles_available()  # no exception


# ---------------------------------------------------------------------------
# Round 3: File filtering
# ---------------------------------------------------------------------------


def test_filter_includes_py_under_source_roots() -> None:
    changed = frozenset({Path("/project/src/pkg/specs.py")})
    result = filter_spec_files(changed, source_roots=[Path("/project/src")], test_roots=[])
    assert result == changed


def test_filter_excludes_non_py() -> None:
    changed = frozenset({Path("/project/src/readme.md"), Path("/project/src/data.json")})
    result = filter_spec_files(changed, source_roots=[Path("/project/src")], test_roots=[])
    assert result == frozenset()


def test_filter_excludes_outside_roots() -> None:
    changed = frozenset({Path("/other/mod.py")})
    result = filter_spec_files(
        changed, source_roots=[Path("/project/src")], test_roots=[Path("/project/tests")]
    )
    assert result == frozenset()


def test_filter_includes_test_roots() -> None:
    changed = frozenset({Path("/project/tests/test_foo.py")})
    result = filter_spec_files(
        changed, source_roots=[Path("/project/src")], test_roots=[Path("/project/tests")]
    )
    assert result == changed


def test_filter_excludes_generated_dir() -> None:
    changed = frozenset({Path("/project/src/pkg/__generated__/specs.py")})
    result = filter_spec_files(changed, source_roots=[Path("/project/src")], test_roots=[])
    assert result == frozenset()


def test_filter_mixed_paths() -> None:
    """Mix of valid and invalid paths; only valid ones pass."""
    changed = frozenset(
        {
            Path("/project/src/pkg/specs.py"),  # valid
            Path("/project/src/readme.md"),  # not .py
            Path("/other/mod.py"),  # outside roots
            Path("/project/src/pkg/__generated__/out.py"),  # generated
            Path("/project/tests/test_a.py"),  # valid (test root)
        }
    )
    result = filter_spec_files(
        changed,
        source_roots=[Path("/project/src")],
        test_roots=[Path("/project/tests")],
    )
    assert result == frozenset(
        {Path("/project/src/pkg/specs.py"), Path("/project/tests/test_a.py")}
    )


def test_filter_mixed_python_typescript_roots_and_custom_generated_dirs() -> None:
    changed = frozenset(
        {
            Path("/project/python/pkg/spec.py"),
            Path("/project/python/pkg/py-machine/out.py"),
            Path("/project/web/src/tokens/index.jaunt.ts"),
            Path("/project/web/tests/tokens.jaunt-test.ts"),
            Path("/project/web/src/tokens/ts-machine/index.ts"),
            Path("/project/package-lock.json"),
            Path("/project/packages/web/tsconfig.build.json"),
            Path("/project/node_modules/typescript/package.json"),
        }
    )

    result = filter_spec_files(
        changed,
        source_roots=[Path("/project/python")],
        test_roots=[],
        generated_dir="py-machine",
        typescript_source_roots=[Path("/project/web/src")],
        typescript_test_roots=[Path("/project/web/tests")],
        typescript_generated_dir="ts-machine",
        workspace_root=Path("/project"),
    )

    assert result == frozenset(
        {
            Path("/project/python/pkg/spec.py"),
            Path("/project/web/src/tokens/index.jaunt.ts"),
            Path("/project/web/tests/tokens.jaunt-test.ts"),
            Path("/project/package-lock.json"),
            Path("/project/packages/web/tsconfig.build.json"),
        }
    )


def test_filter_includes_exact_referenced_config_outside_target_roots() -> None:
    config = Path("/project/configs/tsconfig.shared.json")
    result = filter_spec_files(
        frozenset({config}),
        source_roots=[],
        test_roots=[],
        typescript_source_roots=[Path("/project/packages/app/src")],
        workspace_root=Path("/project"),
        config_paths=[config],
    )

    assert result == frozenset({config})


# ---------------------------------------------------------------------------
# Round 4: Watch loop orchestration
# ---------------------------------------------------------------------------


async def _fake_changes(
    batches: list[set[tuple[Any, str]]],
) -> AsyncIterator[set[tuple[Any, str]]]:
    for batch in batches:
        yield batch


async def _run_watch_once(
    *,
    batch: set[tuple[Any, str]],
    run_cycle: Any,
    on_event: Any = lambda msg: None,
    on_cycle_result: Any = lambda r: None,
    on_error: Any = lambda e: None,
    source_roots: list[Path] | None = None,
    test_roots: list[Path] | None = None,
) -> None:
    await run_watch_loop(
        changes_iter=_fake_changes([batch]),
        run_cycle=run_cycle,
        on_event=on_event,
        on_cycle_result=on_cycle_result,
        on_error=on_error,
        source_roots=source_roots or [Path("/src")],
        test_roots=test_roots or [],
    )


def test_watch_loop_calls_run_cycle_on_change() -> None:
    cycles: list[WatchEvent] = []

    def fake_run_cycle(event: WatchEvent) -> WatchCycleResult:
        cycles.append(event)
        return WatchCycleResult(
            build_exit_code=0,
            test_exit_code=None,
            duration_s=0.5,
            changed_paths=event.changed_paths,
        )

    async def run() -> None:
        await _run_watch_once(
            batch={(1, "/src/pkg/specs.py")},
            run_cycle=fake_run_cycle,
        )

    asyncio.run(run())
    assert len(cycles) == 1
    assert Path("/src/pkg/specs.py") in cycles[0].changed_paths


def test_watch_loop_skips_irrelevant_changes() -> None:
    cycles: list[WatchEvent] = []

    def fake_run_cycle(event: WatchEvent) -> WatchCycleResult:
        cycles.append(event)
        return WatchCycleResult(
            build_exit_code=0,
            test_exit_code=None,
            duration_s=0.1,
            changed_paths=event.changed_paths,
        )

    async def run() -> None:
        await _run_watch_once(
            batch={(1, "/other/readme.md")},
            run_cycle=fake_run_cycle,
        )

    asyncio.run(run())
    assert len(cycles) == 0


def test_watch_loop_emits_change_detected_message() -> None:
    messages: list[str] = []

    def fake_run_cycle(event: WatchEvent) -> WatchCycleResult:
        return WatchCycleResult(
            build_exit_code=0,
            test_exit_code=None,
            duration_s=0.3,
            changed_paths=event.changed_paths,
        )

    async def run() -> None:
        await _run_watch_once(
            batch={(1, "/src/pkg/specs.py")},
            run_cycle=fake_run_cycle,
            on_event=lambda msg: messages.append(msg),
        )

    asyncio.run(run())
    assert any("change detected" in m for m in messages)
    assert any("specs.py" in m for m in messages)


def test_watch_loop_emits_building_and_done_messages() -> None:
    messages: list[str] = []

    def fake_run_cycle(event: WatchEvent) -> WatchCycleResult:
        return WatchCycleResult(
            build_exit_code=0,
            test_exit_code=None,
            duration_s=0.8,
            changed_paths=event.changed_paths,
        )

    async def run() -> None:
        await _run_watch_once(
            batch={(1, "/src/a.py")},
            run_cycle=fake_run_cycle,
            on_event=lambda msg: messages.append(msg),
        )

    asyncio.run(run())
    assert any("building" in m for m in messages)
    assert any("done" in m for m in messages)


def test_watch_loop_continues_after_build_failure() -> None:
    cycles: list[WatchEvent] = []

    def fake_run_cycle(event: WatchEvent) -> WatchCycleResult:
        cycles.append(event)
        return WatchCycleResult(
            build_exit_code=3,
            test_exit_code=None,
            duration_s=0.2,
            changed_paths=event.changed_paths,
        )

    async def run() -> None:
        await run_watch_loop(
            changes_iter=_fake_changes(
                [
                    {(1, "/src/a.py")},
                    {(1, "/src/b.py")},
                ]
            ),
            run_cycle=fake_run_cycle,
            on_event=lambda msg: None,
            on_cycle_result=lambda r: None,
            on_error=lambda e: None,
            source_roots=[Path("/src")],
            test_roots=[],
        )

    asyncio.run(run())
    assert len(cycles) == 2


def test_watch_loop_handles_exception_in_run_cycle() -> None:
    errors: list[BaseException] = []
    call_count = 0

    def exploding_run_cycle(event: WatchEvent) -> WatchCycleResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        return WatchCycleResult(
            build_exit_code=0,
            test_exit_code=None,
            duration_s=0.1,
            changed_paths=event.changed_paths,
        )

    async def run() -> None:
        await run_watch_loop(
            changes_iter=_fake_changes(
                [
                    {(1, "/src/a.py")},
                    {(1, "/src/b.py")},
                ]
            ),
            run_cycle=exploding_run_cycle,
            on_event=lambda msg: None,
            on_cycle_result=lambda r: None,
            on_error=lambda e: errors.append(e),
            source_roots=[Path("/src")],
            test_roots=[],
        )

    asyncio.run(run())
    assert len(errors) == 1
    assert call_count == 2


def test_watch_loop_reports_cycle_result() -> None:
    results: list[WatchCycleResult] = []

    def fake_run_cycle(event: WatchEvent) -> WatchCycleResult:
        return WatchCycleResult(
            build_exit_code=0,
            test_exit_code=None,
            duration_s=0.5,
            changed_paths=event.changed_paths,
        )

    async def run() -> None:
        await _run_watch_once(
            batch={(1, "/src/a.py")},
            run_cycle=fake_run_cycle,
            on_cycle_result=lambda r: results.append(r),
        )

    asyncio.run(run())
    assert len(results) == 1
    assert results[0].build_exit_code == 0


def test_watch_loop_awaits_async_cycle_runner() -> None:
    messages: list[str] = []
    results: list[WatchCycleResult] = []
    errors: list[BaseException] = []
    seen_events: list[WatchEvent] = []

    async def fake_run_cycle(event: WatchEvent) -> WatchCycleResult:
        seen_events.append(event)
        await asyncio.sleep(0)
        return WatchCycleResult(
            build_exit_code=0,
            test_exit_code=None,
            duration_s=0.4,
            changed_paths=event.changed_paths,
        )

    async def run() -> None:
        await _run_watch_once(
            batch={(1, "/src/pkg/specs.py")},
            run_cycle=fake_run_cycle,
            on_event=lambda msg: messages.append(msg),
            on_cycle_result=lambda r: results.append(r),
            on_error=lambda e: errors.append(e),
        )

    asyncio.run(run())
    assert len(seen_events) == 1
    assert len(results) == 1
    assert errors == []
    assert any("building" in msg for msg in messages)
    assert any("done" in msg for msg in messages)


# ---------------------------------------------------------------------------
# Round 5: JSON output formatting
# ---------------------------------------------------------------------------


def test_format_json_build_only() -> None:
    result = WatchCycleResult(
        build_exit_code=0,
        test_exit_code=None,
        duration_s=0.8,
        changed_paths=frozenset({Path("/src/a.py")}),
    )
    data = format_watch_cycle_json(result)
    assert data["command"] == "watch"
    assert data["ok"] is True
    assert data["build_exit_code"] == 0
    assert data["test_exit_code"] is None
    assert data["duration_s"] == 0.8
    assert data["changed_paths"] == ["/src/a.py"]


def test_format_json_with_test_failure() -> None:
    result = WatchCycleResult(
        build_exit_code=0,
        test_exit_code=4,
        duration_s=1.2,
        changed_paths=frozenset({Path("/src/a.py")}),
    )
    data = format_watch_cycle_json(result)
    assert data["ok"] is False
    assert data["test_exit_code"] == 4


def test_format_json_build_failure() -> None:
    result = WatchCycleResult(
        build_exit_code=3,
        test_exit_code=None,
        duration_s=0.5,
        changed_paths=frozenset({Path("/src/a.py")}),
    )
    data = format_watch_cycle_json(result)
    assert data["ok"] is False
    assert data["build_exit_code"] == 3


# ---------------------------------------------------------------------------
# Round 6: Cycle runner
# ---------------------------------------------------------------------------


def test_cycle_runner_calls_cmd_build(monkeypatch) -> None:
    build_calls: list[bool] = []

    async def fake_cmd_build(args: Any) -> int:
        build_calls.append(bool(args.json_output))
        return 0

    monkeypatch.setattr(
        "jaunt.cli._cmd_build_async",
        fake_cmd_build,
    )

    import jaunt.cli

    ns = jaunt.cli.parse_args(["watch", "--json"])
    runner = build_cycle_runner(ns, run_tests=False)

    event = WatchEvent(changed_paths=frozenset({Path("/src/a.py")}), timestamp=1000.0)
    result = asyncio.run(runner(event))
    assert result.build_exit_code == 0
    assert result.test_exit_code is None
    assert build_calls == [False]


def test_cycle_runner_calls_cmd_test_when_enabled(monkeypatch) -> None:
    build_calls: list[bool] = []
    test_calls: list[tuple[bool, bool, bool, list[str]]] = []

    async def fake_cmd_build(args: Any) -> int:
        build_calls.append(bool(args.json_output))
        return 0

    monkeypatch.setattr("jaunt.cli._cmd_build_async", fake_cmd_build)

    async def fake_cmd_test(args: Any) -> int:
        test_calls.append(
            (
                bool(args.no_build),
                bool(args.no_run),
                bool(args.json_output),
                list(args.pytest_args),
            )
        )
        return 0

    monkeypatch.setattr("jaunt.cli._cmd_test_workspace_async", fake_cmd_test)

    import jaunt.cli

    ns = jaunt.cli.parse_args(["watch", "--test", "--json"])
    runner = build_cycle_runner(ns, run_tests=True)

    event = WatchEvent(changed_paths=frozenset({Path("/src/a.py")}), timestamp=1000.0)
    result = asyncio.run(runner(event))
    assert result.build_exit_code == 0
    assert result.test_exit_code == 0
    assert len(build_calls) == 1
    assert len(test_calls) == 1
    assert build_calls == [False]
    assert test_calls[0] == (True, False, False, [])


def test_cycle_runner_propagates_progress_mode(monkeypatch) -> None:
    build_calls: list[tuple[str, bool]] = []
    test_calls: list[tuple[str, bool]] = []

    async def fake_cmd_build(args: Any) -> int:
        build_calls.append((args.progress, bool(args.no_progress)))
        return 0

    async def fake_cmd_test(args: Any) -> int:
        test_calls.append((args.progress, bool(args.no_progress)))
        return 0

    monkeypatch.setattr("jaunt.cli._cmd_build_async", fake_cmd_build)
    monkeypatch.setattr("jaunt.cli._cmd_test_workspace_async", fake_cmd_test)

    import jaunt.cli

    ns = jaunt.cli.parse_args(["watch", "--test", "--progress", "none"])
    runner = build_cycle_runner(ns, run_tests=True)

    event = WatchEvent(changed_paths=frozenset({Path("/src/a.py")}), timestamp=1000.0)
    result = asyncio.run(runner(event))

    assert result.build_exit_code == 0
    assert result.test_exit_code == 0
    assert build_calls == [("none", False)]
    assert test_calls == [("none", False)]


def test_cycle_runner_still_propagates_no_progress_alias(monkeypatch) -> None:
    build_calls: list[tuple[str, bool]] = []
    test_calls: list[tuple[str, bool]] = []

    async def fake_cmd_build(args: Any) -> int:
        build_calls.append((args.progress, bool(args.no_progress)))
        return 0

    async def fake_cmd_test(args: Any) -> int:
        test_calls.append((args.progress, bool(args.no_progress)))
        return 0

    monkeypatch.setattr("jaunt.cli._cmd_build_async", fake_cmd_build)
    monkeypatch.setattr("jaunt.cli._cmd_test_workspace_async", fake_cmd_test)

    import jaunt.cli

    ns = jaunt.cli.parse_args(["watch", "--test", "--progress", "plain", "--no-progress"])
    runner = build_cycle_runner(ns, run_tests=True)

    event = WatchEvent(changed_paths=frozenset({Path("/src/a.py")}), timestamp=1000.0)
    result = asyncio.run(runner(event))

    assert result.build_exit_code == 0
    assert result.test_exit_code == 0
    assert build_calls == [("plain", True)]
    assert test_calls == [("plain", True)]


def test_cycle_runner_skips_test_on_build_failure(monkeypatch) -> None:
    async def fake_cmd_build(args: Any) -> int:
        return 3

    test_calls: list[object] = []

    async def fake_cmd_test(args: Any) -> int:
        test_calls.append(args)
        return 0

    monkeypatch.setattr("jaunt.cli._cmd_build_async", fake_cmd_build)
    monkeypatch.setattr("jaunt.cli._cmd_test_workspace_async", fake_cmd_test)

    import jaunt.cli

    ns = jaunt.cli.parse_args(["watch", "--test"])
    runner = build_cycle_runner(ns, run_tests=True)

    event = WatchEvent(changed_paths=frozenset({Path("/src/a.py")}), timestamp=1000.0)
    result = asyncio.run(runner(event))
    assert result.build_exit_code == 3
    assert result.test_exit_code is None
    assert len(test_calls) == 0


def test_cycle_runner_measures_duration(monkeypatch) -> None:
    async def fake_cmd_build(args: Any) -> int:
        return 0

    monkeypatch.setattr("jaunt.cli._cmd_build_async", fake_cmd_build)

    import jaunt.cli

    ns = jaunt.cli.parse_args(["watch"])
    runner = build_cycle_runner(ns, run_tests=False)

    event = WatchEvent(changed_paths=frozenset({Path("/src/a.py")}), timestamp=1000.0)
    result = asyncio.run(runner(event))
    assert result.duration_s >= 0.0


def test_cycle_runner_runs_typescript_build_and_test_without_nested_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jaunt.targets.base import TargetBuildReport, TargetTestReport

    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        """\
version = 2

[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
""",
        encoding="utf-8",
    )
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def fake_build(_root, _config, *, target_ids, **_kwargs):
        calls.append(("build", tuple(target_ids)))
        return TargetBuildReport(language="ts", generated=frozenset(target_ids))

    async def fake_test(_root, _config, *, target_ids, **_kwargs):
        calls.append(("test", tuple(target_ids)))
        return TargetTestReport(language="ts", generated=frozenset(target_ids))

    monkeypatch.setattr("jaunt.typescript.builder.run_build", fake_build)
    monkeypatch.setattr("jaunt.typescript.tester.run_test", fake_test)

    import jaunt.cli

    target = "ts:src/tokens/index"
    ns = jaunt.cli.parse_args(
        [
            "watch",
            "--test",
            "--language",
            "ts",
            "--target",
            target,
            "--root",
            str(tmp_path),
        ]
    )
    runner = build_cycle_runner(ns, run_tests=True)
    event = WatchEvent(
        changed_paths=frozenset({tmp_path / "src" / "tokens" / "index.jaunt.ts"}),
        timestamp=1000.0,
    )

    result = asyncio.run(runner(event))

    assert result.build_exit_code == 0
    assert result.test_exit_code == 0
    assert calls == [("build", (target,)), ("test", (target,))]


def test_mixed_watch_target_partition_keeps_both_explicit_languages() -> None:
    args = types.SimpleNamespace(target=["api.models", "ts:packages/web/src/token"])

    assert _python_selected(args) is True
    assert _typescript_targets(args) == ("ts:packages/web/src/token",)


@pytest.mark.asyncio
async def test_typescript_watch_reuses_analyzer_and_invalidates_for_100_cycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jaunt.targets.base import TargetBuildReport

    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    spec_path = tmp_path / "src" / "dependency.jaunt.ts"
    spec_path.write_text("export declare function dependency(): void;\n", encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        """\
version = 2
[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
""",
        encoding="utf-8",
    )
    modules = [
        {
            "moduleId": "ts:src/dependency",
            "specPath": "src/dependency.jaunt.ts",
            "project": "tsconfig.json",
            "packageOwner": ".",
            "dependencies": [],
        },
        {
            "moduleId": "ts:src/consumer",
            "specPath": "src/consumer.jaunt.ts",
            "project": "tsconfig.json",
            "packageOwner": ".",
            "dependencies": ["ts:src/dependency#dependency"],
        },
    ]

    class PersistentWorker:
        def __init__(self) -> None:
            self.invalidations: list[tuple[str, ...]] = []

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "invalidate":
                self.invalidations.append(tuple(params["paths"]))
                return {"invalidated": list(params["paths"])}
            if method == "analyzeWorkspace":
                return {
                    "diagnostics": [],
                    "projects": [
                        {
                            "id": "tsconfig.json",
                            "configPath": "tsconfig.json",
                            "references": [],
                            "rootFiles": [
                                "src/dependency.jaunt.ts",
                                "src/consumer.jaunt.ts",
                            ],
                        }
                    ],
                    "testSpecs": [],
                }
            if method == "analyzeContracts":
                return {
                    "sessionId": "watch",
                    "epoch": len(self.invalidations) + 1,
                    "snapshot": f"snapshot-{len(self.invalidations)}",
                    "inputHashes": {},
                    "modules": modules,
                }
            raise AssertionError(method)

    worker = PersistentWorker()
    sessions = {"entered": 0, "exited": 0}

    @asynccontextmanager
    async def persistent_session(*_args, **_kwargs):
        sessions["entered"] += 1
        try:
            yield worker, types.SimpleNamespace()
        finally:
            sessions["exited"] += 1

    built_targets: list[tuple[str, ...]] = []

    async def fake_build(
        _root,
        _config,
        _client,
        _initialized,
        *,
        target_ids,
        **_kwargs,
    ):
        built_targets.append(tuple(target_ids))
        return TargetBuildReport(language="ts", generated=frozenset(target_ids))

    monkeypatch.setattr("jaunt.typescript.builder.worker_session", persistent_session)
    monkeypatch.setattr("jaunt.typescript.builder.run_build_in_session", fake_build)

    async def changes() -> AsyncIterator[set[tuple[int, str]]]:
        for _ in range(100):
            yield {(1, str(spec_path))}

    import jaunt.cli

    runner = build_cycle_runner(
        jaunt.cli.parse_args(["watch", "--language", "ts", "--root", str(tmp_path)]),
        run_tests=False,
    )
    baseline = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    results: list[WatchCycleResult] = []
    await run_watch_loop(
        changes_iter=changes(),
        run_cycle=runner,
        on_event=lambda _message: None,
        on_cycle_result=results.append,
        on_error=lambda error: pytest.fail(str(error)),
        source_roots=[],
        test_roots=[],
        typescript_source_roots=[tmp_path / "src"],
        typescript_test_roots=[tmp_path / "tests"],
        workspace_root=tmp_path,
        config_paths=[tmp_path / "jaunt.toml", tmp_path / "tsconfig.json"],
    )
    await asyncio.sleep(0)

    assert sessions == {"entered": 1, "exited": 1}
    assert len(worker.invalidations) == 100
    assert len(results) == 100
    assert set(built_targets) == {("ts:src/consumer", "ts:src/dependency")}
    remaining = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    assert remaining == baseline
