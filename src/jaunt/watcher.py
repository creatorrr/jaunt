"""Watch mode: rebuild on spec file changes."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class WatchEvent:
    """A batch of relevant file changes."""

    changed_paths: frozenset[Path]
    timestamp: float


@dataclass(frozen=True, slots=True)
class WatchCycleResult:
    """Result of a single watch rebuild cycle."""

    build_exit_code: int
    test_exit_code: int | None
    duration_s: float
    changed_paths: frozenset[Path]


def check_watchfiles_available() -> None:
    """Raise ImportError with a helpful message if watchfiles is not installed."""
    import importlib

    try:
        importlib.import_module("watchfiles")
    except ImportError:
        raise ImportError(
            "watchfiles is required for watch mode. "
            "Install it with: pip install jaunt[watch]"
        ) from None


def filter_spec_files(
    changed_paths: frozenset[Path],
    *,
    source_roots: list[Path],
    test_roots: list[Path],
) -> frozenset[Path]:
    """Filter changed paths to .py files under source/test roots, excluding __generated__."""
    roots = [*source_roots, *test_roots]
    kept: set[Path] = set()
    for p in changed_paths:
        if p.suffix != ".py":
            continue
        if "__generated__" in p.parts:
            continue
        if any(p.is_relative_to(r) for r in roots):
            kept.add(p)
    return frozenset(kept)


async def run_watch_loop(
    *,
    changes_iter: AsyncIterator[set[tuple[Any, str]]],
    run_cycle: Callable[[WatchEvent], WatchCycleResult],
    on_event: Callable[[str], None],
    on_cycle_result: Callable[[WatchCycleResult], None],
    on_error: Callable[[BaseException], None],
    source_roots: list[Path],
    test_roots: list[Path],
) -> None:
    """Main watch loop. Consumes changes_iter, filters, and calls run_cycle."""
    async for raw_changes in changes_iter:
        paths = frozenset(Path(p) for _, p in raw_changes)
        relevant = filter_spec_files(paths, source_roots=source_roots, test_roots=test_roots)
        if not relevant:
            continue

        event = WatchEvent(changed_paths=relevant, timestamp=time.monotonic())

        names = ", ".join(str(p) for p in sorted(relevant))
        on_event(f"[watch] change detected: {names}")
        on_event("[watch] building...")

        try:
            result = run_cycle(event)
        except Exception as exc:
            on_error(exc)
            continue

        on_event(f"[watch] done ({result.duration_s:.1f}s)")
        on_cycle_result(result)


def format_watch_cycle_json(result: WatchCycleResult) -> dict[str, object]:
    """Format a cycle result as a JSON-serializable dict."""
    ok = result.build_exit_code == 0 and result.test_exit_code in (None, 0)
    return {
        "command": "watch",
        "ok": ok,
        "build_exit_code": result.build_exit_code,
        "test_exit_code": result.test_exit_code,
        "duration_s": round(result.duration_s, 2),
        "changed_paths": sorted(str(p) for p in result.changed_paths),
    }


def build_cycle_runner(
    args: Any,
    *,
    run_tests: bool,
) -> Callable[[WatchEvent], WatchCycleResult]:
    """Create a cycle runner that calls cmd_build() and optionally cmd_test()."""
    from jaunt.cli import cmd_build, cmd_test

    def runner(event: WatchEvent) -> WatchCycleResult:
        t0 = time.monotonic()
        build_rc = cmd_build(args)

        test_rc: int | None = None
        if run_tests and build_rc == 0:
            test_rc = cmd_test(args)

        duration = time.monotonic() - t0
        return WatchCycleResult(
            build_exit_code=build_rc,
            test_exit_code=test_rc,
            duration_s=duration,
            changed_paths=event.changed_paths,
        )

    return runner


def make_watchfiles_iter(
    watch_paths: list[Path],
) -> AsyncIterator[set[tuple[Any, str]]]:
    """Create an async iterator using watchfiles.awatch()."""
    import watchfiles

    return watchfiles.awatch(*watch_paths, debounce=200)
