"""Vigil in the forests of the night -- watch mode, rebuild on spec changes."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
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


@dataclass(frozen=True, slots=True)
class WatchScope:
    """Paths and generated-output rules used to filter one watch batch."""

    source_roots: tuple[Path, ...] = ()
    test_roots: tuple[Path, ...] = ()
    generated_dir: str = "__generated__"
    typescript_source_roots: tuple[Path, ...] = ()
    typescript_test_roots: tuple[Path, ...] = ()
    typescript_generated_dir: str = "__generated__"
    workspace_root: Path | None = None
    config_paths: tuple[Path, ...] = ()


def check_watchfiles_available() -> None:
    """Raise ImportError with a helpful message if watchfiles is not installed."""
    import importlib

    try:
        importlib.import_module("watchfiles")
    except ImportError:
        raise ImportError(
            "watchfiles is required for watch mode but is not available. "
            "Reinstall jaunt; watchfiles is now a core dependency."
        ) from None


def filter_spec_files(
    changed_paths: frozenset[Path],
    *,
    source_roots: Sequence[Path],
    test_roots: Sequence[Path],
    generated_dir: str = "__generated__",
    typescript_source_roots: Sequence[Path] = (),
    typescript_test_roots: Sequence[Path] = (),
    typescript_generated_dir: str = "__generated__",
    workspace_root: Path | None = None,
    config_paths: Sequence[Path] = (),
) -> frozenset[Path]:
    """Keep source and fingerprint inputs while excluding target-owned outputs."""

    roots = [*source_roots, *test_roots]
    ts_roots = [*typescript_source_roots, *typescript_test_roots]
    exact_configs = {path.resolve() for path in config_paths}
    ignored_parts = {".git", ".jaunt", "node_modules", "coverage", "dist"}
    lockfiles = {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "pnpm-workspace.yaml",
    }

    def under(path: Path, candidates: Sequence[Path]) -> bool:
        return any(path.is_relative_to(candidate) for candidate in candidates)

    def generated(path: Path, candidates: Sequence[Path], name: str) -> bool:
        if not name:
            return False
        needle = Path(name).parts
        for candidate in candidates:
            try:
                parts = path.relative_to(candidate).parts
            except ValueError:
                continue
            if any(parts[index : index + len(needle)] == needle for index in range(len(parts))):
                return True
        return False

    kept: set[Path] = set()
    for p in changed_paths:
        resolved = p.resolve()
        if ignored_parts.intersection(resolved.parts):
            continue
        if generated(resolved, roots, generated_dir) or generated(
            resolved,
            ts_roots,
            typescript_generated_dir,
        ):
            continue
        if resolved.suffix == ".py" and under(resolved, roots):
            kept.add(p)
            continue
        if resolved.suffix in {".ts", ".tsx"} and under(resolved, ts_roots):
            kept.add(p)
            continue
        in_workspace = workspace_root is not None and resolved.is_relative_to(
            workspace_root.resolve()
        )
        if resolved in exact_configs or (
            in_workspace
            and (
                resolved.name == "jaunt.toml"
                or resolved.name == "package.json"
                or resolved.name in lockfiles
                or (resolved.name.startswith("tsconfig") and resolved.suffix == ".json")
            )
        ):
            kept.add(p)
    return frozenset(kept)


async def run_watch_loop(
    *,
    changes_iter: AsyncIterator[set[tuple[Any, str]]],
    run_cycle: Callable[[WatchEvent], WatchCycleResult | Awaitable[WatchCycleResult]],
    on_event: Callable[[str], None],
    on_cycle_result: Callable[[WatchCycleResult], None],
    on_error: Callable[[BaseException], None],
    source_roots: Sequence[Path],
    test_roots: Sequence[Path],
    generated_dir: str = "__generated__",
    typescript_source_roots: Sequence[Path] = (),
    typescript_test_roots: Sequence[Path] = (),
    typescript_generated_dir: str = "__generated__",
    workspace_root: Path | None = None,
    config_paths: Sequence[Path] = (),
    watch_scope_provider: Callable[[], WatchScope] | None = None,
) -> None:
    """Main watch loop. Consumes changes_iter, filters, and calls run_cycle."""
    watch_scope = WatchScope(
        source_roots=tuple(source_roots),
        test_roots=tuple(test_roots),
        generated_dir=generated_dir,
        typescript_source_roots=tuple(typescript_source_roots),
        typescript_test_roots=tuple(typescript_test_roots),
        typescript_generated_dir=typescript_generated_dir,
        workspace_root=workspace_root,
        config_paths=tuple(config_paths),
    )
    active_cycle = run_cycle
    enter = getattr(run_cycle, "__aenter__", None)
    exit_ = getattr(run_cycle, "__aexit__", None)
    entered = False
    try:
        if callable(enter):
            replacement = await enter()
            if replacement is not None:
                active_cycle = replacement
            entered = True
        async for raw_changes in changes_iter:
            paths = frozenset(Path(p) for _, p in raw_changes)
            if watch_scope_provider is not None:
                try:
                    watch_scope = watch_scope_provider()
                except Exception as exc:
                    # A partially-written config must not terminate watch mode or
                    # hide the config event. Keep the last valid scope so the
                    # cycle runner can report the configuration error and a later
                    # valid edit can recover without restarting the process.
                    on_error(exc)
            relevant = filter_spec_files(
                paths,
                source_roots=watch_scope.source_roots,
                test_roots=watch_scope.test_roots,
                generated_dir=watch_scope.generated_dir,
                typescript_source_roots=watch_scope.typescript_source_roots,
                typescript_test_roots=watch_scope.typescript_test_roots,
                typescript_generated_dir=watch_scope.typescript_generated_dir,
                workspace_root=watch_scope.workspace_root,
                config_paths=watch_scope.config_paths,
            )
            if not relevant:
                continue

            event = WatchEvent(changed_paths=relevant, timestamp=time.monotonic())

            names = ", ".join(str(p) for p in sorted(relevant))
            on_event(f"[watch] change detected: {names}")
            on_event("[watch] building...")

            try:
                result = active_cycle(event)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                on_error(exc)
                continue

            on_event(f"[watch] done ({result.duration_s:.1f}s)")
            on_cycle_result(result)
    finally:
        if entered and callable(exit_):
            await exit_(None, None, None)


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


def _workspace_relative(root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _affected_typescript_module_ids(
    root: Path,
    analysis: Any,
    changed_paths: Sequence[Path],
) -> tuple[str, ...]:
    """Return changed modules plus their reverse dependency/project closure."""

    modules = tuple(analysis.modules)
    by_id = {
        str(module.get("moduleId")): module
        for module in modules
        if isinstance(module.get("moduleId"), str)
    }
    if not by_id:
        return ()
    changed = {_workspace_relative(root, path) for path in changed_paths}
    affected: set[str] = set()

    path_fields = ("specPath", "contextPath", "facadePath")
    for module_id, module in by_id.items():
        if any(str(module.get(field, "")) in changed for field in path_fields):
            affected.add(module_id)

    workspace = analysis.workspace
    raw_test_specs = workspace.get("testSpecs", [])
    if isinstance(raw_test_specs, list):
        for test_spec in raw_test_specs:
            if not isinstance(test_spec, dict) or str(test_spec.get("path", "")) not in changed:
                continue
            targets = test_spec.get("targets", [])
            if isinstance(targets, list):
                affected.update(
                    target.split("#", 1)[0]
                    for target in targets
                    if isinstance(target, str) and target.split("#", 1)[0] in by_id
                )

    raw_projects = workspace.get("projects", [])
    projects = (
        [item for item in raw_projects if isinstance(item, dict)]
        if isinstance(raw_projects, list)
        else []
    )
    project_ids = {
        str(project.get("id")) for project in projects if isinstance(project.get("id"), str)
    }
    changed_projects: set[str] = set()
    for project in projects:
        project_id = project.get("id")
        if not isinstance(project_id, str):
            continue
        config_path = str(project.get("configPath", project_id))
        root_files = project.get("rootFiles", [])
        if config_path in changed or (
            isinstance(root_files, list)
            and any(isinstance(path, str) and path in changed for path in root_files)
        ):
            changed_projects.add(project_id)

    package_changes = {
        str(Path(path).parent.as_posix() or ".")
        for path in changed
        if Path(path).name == "package.json"
    }
    global_names = {
        "jaunt.toml",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "pnpm-workspace.yaml",
    }
    global_change = any(
        Path(path).name in global_names and Path(path).parent == Path(".") for path in changed
    )
    if global_change:
        affected.update(by_id)
    for module_id, module in by_id.items():
        owner = str(module.get("packageOwner", "."))
        if owner in package_changes:
            affected.add(module_id)

    # A project-config change invalidates its production reference component.
    # A changed solution config fans out to its roots, but a leaf change does
    # not use that solution-only edge to couple otherwise unrelated siblings.
    for project in projects:
        project_id = project.get("id")
        references = project.get("references", [])
        if (
            project.get("role") == "solution"
            and isinstance(project_id, str)
            and project_id in changed_projects
            and isinstance(references, list)
        ):
            changed_projects.update(item for item in references if isinstance(item, str))
    while True:
        previous = len(changed_projects)
        for project in projects:
            project_id = project.get("id")
            references = project.get("references", [])
            if (
                project.get("role") == "solution"
                or not isinstance(project_id, str)
                or not isinstance(references, list)
            ):
                continue
            string_references = {item for item in references if isinstance(item, str)}
            if project_id in changed_projects:
                changed_projects.update(string_references)
            elif string_references.intersection(changed_projects):
                changed_projects.add(project_id)
        if len(changed_projects) == previous:
            break
    affected.update(
        module_id
        for module_id, module in by_id.items()
        if str(module.get("project", "")) in changed_projects
    )

    # If a handwritten TS input could not be mapped exactly, use its nearest
    # owning project. This keeps imported type/context changes conservative.
    unmatched_ts = [path for path in changed if Path(path).suffix in {".ts", ".tsx"}]
    for relative in unmatched_ts:
        if any(
            relative == str(module.get(field, "")) for module in modules for field in path_fields
        ):
            continue
        owners = [
            project_id
            for project_id in project_ids
            if Path(relative).is_relative_to(Path(project_id).parent)
        ]
        if owners:
            owner = max(owners, key=lambda value: len(Path(value).parent.parts))
            affected.update(
                module_id
                for module_id, module in by_id.items()
                if str(module.get("project", "")) == owner
            )

    # Prompt templates and other exact watch inputs are not present in the IR.
    # Conservatively rebuild all TS modules rather than dropping such a cycle.
    if not affected and any(Path(path).suffix != ".py" for path in changed):
        affected.update(by_id)

    reverse_dependencies: dict[str, set[str]] = {module_id: set() for module_id in by_id}
    for module_id, module in by_id.items():
        dependencies = module.get("dependencies", [])
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            if not isinstance(dependency, str):
                continue
            dependency_id = dependency.split("#", 1)[0]
            if dependency_id in reverse_dependencies:
                reverse_dependencies[dependency_id].add(module_id)
    pending = list(affected)
    while pending:
        dependency = pending.pop()
        for dependent in reverse_dependencies.get(dependency, set()):
            if dependent not in affected:
                affected.add(dependent)
                pending.append(dependent)
    return tuple(sorted(affected))


class _WatchCycleRunner:
    """Callable cycle runner that owns one analyzer for the watch lifetime."""

    def __init__(
        self,
        args: Any,
        build_args: argparse.Namespace,
        test_args: argparse.Namespace,
        *,
        run_tests: bool,
    ) -> None:
        self.args = args
        self.build_args = build_args
        self.test_args = test_args
        self.run_tests = run_tests
        self._worker_context: Any = None
        self._typescript_session: tuple[Any, Any] | None = None
        self._root: Path | None = None
        self._config: Any = None
        self._typescript_repo_map_block: str | None = None
        self._typescript_builtin_skills: tuple[str, ...] | None = None
        self._typescript_generation_fingerprint: str | None = None

    async def __aenter__(self) -> _WatchCycleRunner:
        await self._open_typescript_session()
        return self

    async def _open_typescript_session(self) -> None:
        from jaunt.cli import _load_config, _target_dispatch_mode

        root, config = _load_config(self.args)
        mode = _target_dispatch_mode(self.args, config)
        targets = _typescript_targets(self.args)
        if mode == "ts":
            from jaunt.cli import _typescript_target_ids

            targets = _typescript_target_ids(self.args)
        self._root = root
        self._config = config
        if mode in {"ts", "mixed"} and targets is not None:
            from jaunt.cli import _effective_build_instructions
            from jaunt.typescript.builder import _generation_fingerprint, _target, worker_session

            builtin_skills = (
                tuple(config.skills.builtin_skills)
                if config.skills.builtin
                and not bool(getattr(self.args, "no_builtin_skills", False))
                else ()
            )
            if config.skills.auto and not bool(getattr(self.args, "no_auto_skills", False)):
                from jaunt.skills_npm import ensure_npm_skills, typescript_package_owners

                ensure_npm_skills(
                    project_root=root,
                    package_owners=typescript_package_owners(root, _target(config)),
                    max_readme_chars=config.skills.max_chars_per_skill,
                )
            use_repo_map = config.context.repo_map and not bool(
                getattr(self.args, "no_repo_map", False)
            )
            repo_map_block = ""
            if use_repo_map:
                from datetime import date

                from jaunt.repo_context.api import repo_map_block_for_build

                repo_map_block = repo_map_block_for_build(
                    root=root,
                    cfg=config,
                    today=date.today().isoformat(),
                )
            generation_fingerprint = _generation_fingerprint(
                config,
                root=root,
                build_instructions=_effective_build_instructions(config, self.build_args),
                builtin_skill_names=builtin_skills,
                repo_map_enabled=use_repo_map,
                project_overview_enabled=bool(config.context.overview),
            )
            self._typescript_repo_map_block = repo_map_block
            self._typescript_builtin_skills = builtin_skills
            self._typescript_generation_fingerprint = generation_fingerprint

            self._worker_context = worker_session(
                root,
                config,
                generation_fingerprint=generation_fingerprint,
            )
            self._typescript_session = await self._worker_context.__aenter__()

    async def _restart_typescript_session(self) -> None:
        if self._worker_context is not None:
            await self._worker_context.__aexit__(None, None, None)
        self._typescript_session = None
        self._worker_context = None
        await self._open_typescript_session()

    async def __aexit__(self, *exc: object) -> None:
        if self._worker_context is not None:
            await self._worker_context.__aexit__(*exc)
        self._typescript_session = None
        self._worker_context = None

    async def __call__(self, event: WatchEvent) -> WatchCycleResult:
        t0 = time.monotonic()
        target_override: tuple[str, ...] | None = None
        run_typescript: bool | None = None
        if self._typescript_session is not None:
            from jaunt.typescript.builder import analyze

            if any(
                path.suffix.lower() not in {".py", ".ts", ".tsx"} for path in event.changed_paths
            ):
                # Compiler/package/Jaunt config and prompt changes alter the
                # initialize fingerprint or tool identity. Start one fresh
                # long-lived session for the new environment.
                await self._restart_typescript_session()
                if self._typescript_session is None:
                    raise RuntimeError("TypeScript watch session was not reinitialized")
            client, initialized = self._typescript_session
            root = self._root or Path.cwd()
            relative_paths = sorted(_workspace_relative(root, path) for path in event.changed_paths)
            await client.request("invalidate", {"paths": relative_paths})
            analysis = await analyze(client, initialized)
            affected = _affected_typescript_module_ids(root, analysis, tuple(event.changed_paths))
            requested = _typescript_targets(self.args)
            if requested:
                requested_modules = {target.split("#", 1)[0] for target in requested}
                run_typescript = bool(requested_modules.intersection(affected))
                target_override = requested
            else:
                target_override = affected
                run_typescript = bool(affected)

        build_rc = await _run_target_build(
            self.build_args,
            typescript_session=self._typescript_session,
            typescript_targets=target_override,
            run_typescript=run_typescript,
            typescript_repo_map_block=self._typescript_repo_map_block,
            typescript_builtin_skills=self._typescript_builtin_skills,
            typescript_generation_fingerprint=self._typescript_generation_fingerprint,
        )

        test_rc: int | None = None
        if self.run_tests and build_rc == 0:
            if self._typescript_session is not None and run_typescript is not False:
                # The build transaction wrote validated artifacts after the
                # analyzer snapshot was taken. Refresh the same process before
                # Vitest/typecheck consumes those files.
                client, _initialized = self._typescript_session
                await client.request("invalidate", {"paths": []})
            test_rc = await _run_target_test(
                self.test_args,
                typescript_session=self._typescript_session,
                typescript_targets=target_override,
                run_typescript=run_typescript,
                typescript_repo_map_block=self._typescript_repo_map_block,
                typescript_builtin_skills=self._typescript_builtin_skills,
            )

        return WatchCycleResult(
            build_exit_code=build_rc,
            test_exit_code=test_rc,
            duration_s=time.monotonic() - t0,
            changed_paths=event.changed_paths,
        )


def build_cycle_runner(
    args: Any,
    *,
    run_tests: bool,
) -> Callable[[WatchEvent], Coroutine[Any, Any, WatchCycleResult]]:
    """Create a target-aware runner without nesting ``asyncio.run`` calls."""

    build_args = argparse.Namespace(
        root=getattr(args, "root", None),
        config=getattr(args, "config", None),
        jobs=getattr(args, "jobs", None),
        force=bool(getattr(args, "force", False)),
        target=list(getattr(args, "target", [])),
        no_infer_deps=bool(getattr(args, "no_infer_deps", False)),
        no_progress=bool(getattr(args, "no_progress", False)),
        progress=getattr(args, "progress", "auto") or "auto",
        no_cache=bool(getattr(args, "no_cache", False)),
        no_repo_map=bool(getattr(args, "no_repo_map", False)),
        no_auto_skills=bool(getattr(args, "no_auto_skills", False)),
        no_builtin_skills=bool(getattr(args, "no_builtin_skills", False)),
        no_semantic_gate=bool(getattr(args, "no_semantic_gate", False)),
        instructions=list(getattr(args, "instructions", []) or []),
        include_target_tests=getattr(args, "include_target_tests", None),
        language=getattr(args, "language", None),
        json_output=False,
    )

    test_args = argparse.Namespace(
        root=getattr(args, "root", None),
        config=getattr(args, "config", None),
        jobs=getattr(args, "jobs", None),
        force=bool(getattr(args, "force", False)),
        target=list(getattr(args, "target", [])),
        no_infer_deps=bool(getattr(args, "no_infer_deps", False)),
        no_progress=bool(getattr(args, "no_progress", False)),
        progress=getattr(args, "progress", "auto") or "auto",
        no_cache=bool(getattr(args, "no_cache", False)),
        no_repo_map=bool(getattr(args, "no_repo_map", False)),
        no_auto_skills=bool(getattr(args, "no_auto_skills", False)),
        no_builtin_skills=bool(getattr(args, "no_builtin_skills", False)),
        json_output=False,
        no_build=True,
        no_run=False,
        pytest_args=[],
        no_redact_derived=False,
        no_semantic_gate=bool(getattr(args, "no_semantic_gate", False)),
        instructions=list(getattr(args, "instructions", []) or []),
        include_target_tests=getattr(args, "include_target_tests", None),
        language=getattr(args, "language", None),
    )

    return _WatchCycleRunner(
        args,
        build_args,
        test_args,
        run_tests=run_tests,
    )


def _python_child(args: argparse.Namespace) -> argparse.Namespace:
    child = argparse.Namespace(**vars(args))
    child.language = "py"
    child.target = [
        value
        for value in list(getattr(args, "target", []) or [])
        if not str(value).startswith("ts:")
    ]
    return child


def _typescript_targets(args: object) -> tuple[str, ...] | None:
    values = tuple(str(value) for value in (getattr(args, "target", []) or []))
    selected = tuple(value for value in values if value.startswith("ts:"))
    return None if values and not selected else selected


def _python_selected(args: object) -> bool:
    values = tuple(str(value) for value in (getattr(args, "target", []) or []))
    return not values or any(not value.startswith("ts:") for value in values)


async def _run_target_build(
    args: argparse.Namespace,
    *,
    typescript_session: tuple[Any, Any] | None = None,
    typescript_targets: tuple[str, ...] | None = None,
    run_typescript: bool | None = None,
    typescript_repo_map_block: str | None = None,
    typescript_builtin_skills: tuple[str, ...] | None = None,
    typescript_generation_fingerprint: str | None = None,
) -> int:
    from jaunt.cli import _cmd_build_async, _load_config, _target_dispatch_mode
    from jaunt.errors import JauntConfigError, JauntDiscoveryError, JauntGenerationError
    from jaunt.targets.orchestrator import aggregate_exit_code

    try:
        root, config = _load_config(args)
        mode = _target_dispatch_mode(args, config)
        codes: list[int] = []
        targets = _typescript_targets(args)
        if mode == "ts":
            from jaunt.cli import _typescript_target_ids

            targets = _typescript_target_ids(args)
        if typescript_targets is not None:
            targets = typescript_targets
        if (
            mode == "mixed"
            and targets is not None
            and run_typescript is not False
            and typescript_session is None
        ):
            from jaunt.typescript.builder import analyze, worker_session

            async with worker_session(root, config) as (client, initialized):
                await analyze(client, initialized, target_ids=targets)
        operations: list[Awaitable[int]] = []
        if mode in {"py", "mixed"} and (mode == "py" or _python_selected(args)):
            operations.append(_cmd_build_async(_python_child(args)))
        if mode in {"ts", "mixed"} and targets is not None and run_typescript is not False:
            from jaunt.cache import ResponseCache
            from jaunt.cli import _effective_build_instructions, _make_progress
            from jaunt.typescript.builder import run_build, run_build_in_session

            async def build_typescript() -> int:
                force = bool(getattr(args, "force", False))
                jobs = getattr(args, "jobs", None)
                instructions = _effective_build_instructions(config, args)
                gate = False if bool(getattr(args, "no_semantic_gate", False)) else None
                response_cache = ResponseCache(
                    root / ".jaunt" / "cache",
                    enabled=not bool(getattr(args, "no_cache", False)),
                )
                progress = _make_progress(
                    args,
                    label="build:ts",
                    total=max(1, len(targets)),
                    json_mode=False,
                )
                builtin_skills = (
                    typescript_builtin_skills
                    if typescript_builtin_skills is not None
                    else (
                        tuple(config.skills.builtin_skills)
                        if config.skills.builtin
                        and not bool(getattr(args, "no_builtin_skills", False))
                        else ()
                    )
                )
                use_repo_map = config.context.repo_map and not bool(
                    getattr(args, "no_repo_map", False)
                )
                if typescript_session is None:
                    report = await run_build(
                        root,
                        config,
                        target_ids=targets,
                        force=force,
                        jobs=jobs,
                        build_instructions=instructions,
                        semantic_gate_enabled=gate,
                        response_cache=response_cache,
                        progress=progress,
                        repo_map_enabled=use_repo_map,
                        repo_map_block_override=typescript_repo_map_block,
                        auto_skills_enabled=not bool(getattr(args, "no_auto_skills", False)),
                        builtin_skill_names=builtin_skills,
                    )
                else:
                    report = await run_build_in_session(
                        root,
                        config,
                        *typescript_session,
                        target_ids=targets,
                        force=force,
                        jobs=jobs,
                        build_instructions=instructions,
                        semantic_gate_enabled=gate,
                        response_cache=response_cache,
                        progress=progress,
                        repo_map_block=(typescript_repo_map_block if use_repo_map else ""),
                        project_overview_enabled=bool(config.context.overview),
                        builtin_skill_names=builtin_skills,
                        generation_fingerprint=typescript_generation_fingerprint,
                    )
                return report.exit_code

            operations.append(build_typescript())
        if operations:
            codes.extend(await asyncio.gather(*operations))
        return aggregate_exit_code(codes)
    except JauntGenerationError:
        return 3
    except (JauntConfigError, JauntDiscoveryError, KeyError):
        return 2


async def _run_target_test(
    args: argparse.Namespace,
    *,
    typescript_session: tuple[Any, Any] | None = None,
    typescript_targets: tuple[str, ...] | None = None,
    run_typescript: bool | None = None,
    typescript_repo_map_block: str | None = None,
    typescript_builtin_skills: tuple[str, ...] | None = None,
) -> int:
    from jaunt.cli import _cmd_test_workspace_async, _load_config, _target_dispatch_mode
    from jaunt.errors import JauntConfigError, JauntDiscoveryError, JauntGenerationError
    from jaunt.targets.orchestrator import aggregate_exit_code

    try:
        root, config = _load_config(args)
        mode = _target_dispatch_mode(args, config)
        codes: list[int] = []
        targets = _typescript_targets(args)
        if mode == "ts":
            from jaunt.cli import _typescript_target_ids

            targets = _typescript_target_ids(args)
        if typescript_targets is not None:
            targets = typescript_targets
        if (
            mode == "mixed"
            and targets is not None
            and run_typescript is not False
            and typescript_session is None
        ):
            from jaunt.typescript.builder import analyze, worker_session

            async with worker_session(root, config) as (client, initialized):
                await analyze(client, initialized, target_ids=targets)
        operations: list[Awaitable[int]] = []
        if mode in {"py", "mixed"} and (mode == "py" or _python_selected(args)):
            operations.append(_cmd_test_workspace_async(_python_child(args)))
        if mode in {"ts", "mixed"} and targets is not None and run_typescript is not False:
            from jaunt.cache import ResponseCache
            from jaunt.cli import _effective_build_instructions, _make_progress
            from jaunt.typescript.tester import run_test

            async def test_typescript() -> int:
                response_cache = ResponseCache(
                    root / ".jaunt" / "cache",
                    enabled=not bool(getattr(args, "no_cache", False)),
                )
                progress = _make_progress(
                    args,
                    label="test:ts",
                    total=max(1, 2 * len(targets)),
                    json_mode=False,
                )
                builtin_skills = (
                    typescript_builtin_skills
                    if typescript_builtin_skills is not None
                    else (
                        tuple(config.skills.builtin_skills)
                        if config.skills.builtin
                        and not bool(getattr(args, "no_builtin_skills", False))
                        else ()
                    )
                )
                report = await run_test(
                    root,
                    config,
                    target_ids=targets,
                    no_build=True,
                    no_run=False,
                    force=bool(getattr(args, "force", False)),
                    jobs=getattr(args, "jobs", None),
                    build_instructions=_effective_build_instructions(config, args),
                    semantic_gate_enabled=(
                        False if bool(getattr(args, "no_semantic_gate", False)) else None
                    ),
                    worker_session_override=typescript_session,
                    response_cache=response_cache,
                    progress=progress,
                    repo_map_enabled=(
                        config.context.repo_map and not bool(getattr(args, "no_repo_map", False))
                    ),
                    repo_map_block_override=typescript_repo_map_block,
                    auto_skills_enabled=not bool(getattr(args, "no_auto_skills", False)),
                    builtin_skill_names=builtin_skills,
                )
                return report.exit_code

            operations.append(test_typescript())
        if operations:
            codes.extend(await asyncio.gather(*operations))
        return aggregate_exit_code(codes)
    except JauntGenerationError:
        return 3
    except (JauntConfigError, JauntDiscoveryError, KeyError):
        return 2


def make_watchfiles_iter(
    watch_paths: list[Path],
) -> AsyncIterator[set[tuple[Any, str]]]:
    """Create an async iterator using watchfiles.awatch()."""
    import watchfiles

    return watchfiles.awatch(*watch_paths, debounce=200)
