"""Tests for jaunt.watcher module."""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator
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

# ---------------------------------------------------------------------------
# Round 2: Optional dependency check
# ---------------------------------------------------------------------------


def test_check_watchfiles_available_raises_when_missing(monkeypatch) -> None:
    """Should raise ImportError with a helpful install message."""
    # Ensure watchfiles is not importable
    monkeypatch.setitem(sys.modules, "watchfiles", None)

    from jaunt.watcher import check_watchfiles_available

    with pytest.raises(ImportError, match="pip install jaunt\\[watch\\]"):
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


# ---------------------------------------------------------------------------
# Round 4: Watch loop orchestration
# ---------------------------------------------------------------------------


async def _fake_changes(
    batches: list[set[tuple[Any, str]]],
) -> AsyncIterator[set[tuple[Any, str]]]:
    for batch in batches:
        yield batch


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
        await run_watch_loop(
            changes_iter=_fake_changes([{(1, "/src/pkg/specs.py")}]),
            run_cycle=fake_run_cycle,
            on_event=lambda msg: None,
            on_cycle_result=lambda r: None,
            on_error=lambda e: None,
            source_roots=[Path("/src")],
            test_roots=[],
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
        await run_watch_loop(
            changes_iter=_fake_changes([{(1, "/other/readme.md")}]),
            run_cycle=fake_run_cycle,
            on_event=lambda msg: None,
            on_cycle_result=lambda r: None,
            on_error=lambda e: None,
            source_roots=[Path("/src")],
            test_roots=[],
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
        await run_watch_loop(
            changes_iter=_fake_changes([{(1, "/src/pkg/specs.py")}]),
            run_cycle=fake_run_cycle,
            on_event=lambda msg: messages.append(msg),
            on_cycle_result=lambda r: None,
            on_error=lambda e: None,
            source_roots=[Path("/src")],
            test_roots=[],
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
        await run_watch_loop(
            changes_iter=_fake_changes([{(1, "/src/a.py")}]),
            run_cycle=fake_run_cycle,
            on_event=lambda msg: messages.append(msg),
            on_cycle_result=lambda r: None,
            on_error=lambda e: None,
            source_roots=[Path("/src")],
            test_roots=[],
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
        await run_watch_loop(
            changes_iter=_fake_changes([{(1, "/src/a.py")}]),
            run_cycle=fake_run_cycle,
            on_event=lambda msg: None,
            on_cycle_result=lambda r: results.append(r),
            on_error=lambda e: None,
            source_roots=[Path("/src")],
            test_roots=[],
        )

    asyncio.run(run())
    assert len(results) == 1
    assert results[0].build_exit_code == 0


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
    build_calls: list[object] = []
    monkeypatch.setattr("jaunt.cli.cmd_build", lambda args: (build_calls.append(args), 0)[1])

    import jaunt.cli

    ns = jaunt.cli.parse_args(["watch"])
    runner = build_cycle_runner(ns, run_tests=False)

    event = WatchEvent(changed_paths=frozenset({Path("/src/a.py")}), timestamp=1000.0)
    result = runner(event)
    assert result.build_exit_code == 0
    assert result.test_exit_code is None
    assert len(build_calls) == 1


def test_cycle_runner_calls_cmd_test_when_enabled(monkeypatch) -> None:
    build_calls: list[object] = []
    test_calls: list[object] = []
    monkeypatch.setattr("jaunt.cli.cmd_build", lambda args: (build_calls.append(args), 0)[1])
    monkeypatch.setattr("jaunt.cli.cmd_test", lambda args: (test_calls.append(args), 0)[1])

    import jaunt.cli

    ns = jaunt.cli.parse_args(["watch", "--test"])
    runner = build_cycle_runner(ns, run_tests=True)

    event = WatchEvent(changed_paths=frozenset({Path("/src/a.py")}), timestamp=1000.0)
    result = runner(event)
    assert result.build_exit_code == 0
    assert result.test_exit_code == 0
    assert len(build_calls) == 1
    assert len(test_calls) == 1


def test_cycle_runner_skips_test_on_build_failure(monkeypatch) -> None:
    monkeypatch.setattr("jaunt.cli.cmd_build", lambda args: 3)
    test_calls: list[object] = []
    monkeypatch.setattr("jaunt.cli.cmd_test", lambda args: (test_calls.append(args), 0)[1])

    import jaunt.cli

    ns = jaunt.cli.parse_args(["watch", "--test"])
    runner = build_cycle_runner(ns, run_tests=True)

    event = WatchEvent(changed_paths=frozenset({Path("/src/a.py")}), timestamp=1000.0)
    result = runner(event)
    assert result.build_exit_code == 3
    assert result.test_exit_code is None
    assert len(test_calls) == 0


def test_cycle_runner_measures_duration(monkeypatch) -> None:
    monkeypatch.setattr("jaunt.cli.cmd_build", lambda args: 0)

    import jaunt.cli

    ns = jaunt.cli.parse_args(["watch"])
    runner = build_cycle_runner(ns, run_tests=False)

    event = WatchEvent(changed_paths=frozenset({Path("/src/a.py")}), timestamp=1000.0)
    result = runner(event)
    assert result.duration_s >= 0.0
