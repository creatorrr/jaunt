"""The assay: test generation and pytest orchestration.

Did he smile his work to see? -- generate tests, then let pytest be the judge.
"""

from __future__ import annotations

import asyncio
import heapq
import importlib.metadata
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jaunt import paths
from jaunt.agent_docs import ensure_agent_docs
from jaunt.builder import _build_expected_names
from jaunt.cache import CacheEntry, ResponseCache, cache_key_from_context
from jaunt.cost import CostTracker
from jaunt.digest import extract_source_segment, module_digest
from jaunt.errors import JauntGenerationError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.header import (
    extract_generation_fingerprint,
    extract_module_context_digest,
    extract_module_digest,
    format_header,
)
from jaunt.module_contract import build_module_contract, test_public_api_only_by_name
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef
from jaunt.validation import validate_test_contract_only, validate_test_generated_source


def _tool_version() -> str:
    try:
        return importlib.metadata.version("jaunt")
    except Exception:
        return "0"


def run_pytest(
    files: list[Path],
    *,
    pytest_args: list[str] | None = None,
    pythonpath: Sequence[Path] | None = None,
    cwd: Path | None = None,
) -> int:
    result = _run_pytest_capture(
        files,
        pytest_args=pytest_args,
        pythonpath=pythonpath,
        cwd=cwd,
    )
    return result.exit_code


@dataclass(frozen=True, slots=True)
class PytestExecution:
    exit_code: int
    stdout: str
    stderr: str


def _run_pytest_capture(
    files: list[Path],
    *,
    pytest_args: list[str] | None = None,
    pythonpath: Sequence[Path] | None = None,
    cwd: Path | None = None,
) -> PytestExecution:
    if not files:
        return PytestExecution(exit_code=0, stdout="", stderr="")

    args = [sys.executable, "-m", "pytest", *(pytest_args or []), *[str(p) for p in files]]
    env = os.environ.copy()
    if pythonpath is not None:
        new_parts: list[str] = []
        for p in pythonpath:
            rp = p.resolve()
            if not rp.exists():
                continue
            new_parts.append(str(rp))

        cur = env.get("PYTHONPATH") or ""
        cur_parts = [x for x in cur.split(os.pathsep) if x] if cur else []

        merged: list[str] = []
        seen: set[str] = set()
        for s in [*new_parts, *cur_parts]:
            if s in seen:
                continue
            merged.append(s)
            seen.add(s)

        if merged:
            env["PYTHONPATH"] = os.pathsep.join(merged)

    proc = subprocess.run(
        args,
        check=False,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return PytestExecution(
        exit_code=int(proc.returncode),
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _normalize_digest(digest: str | None) -> str | None:
    if not digest:
        return None
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1]
    return digest


def _resolve_test_roots(
    *,
    project_dir: Path,
    tests_package: str,
    test_roots: Sequence[Path] | None,
) -> list[Path]:
    if test_roots is None:
        candidates = [(project_dir / tests_package).resolve()]
    else:
        candidates = [Path(root).resolve() for root in test_roots]

    unique: list[Path] = []
    seen: set[Path] = set()
    for root in candidates:
        if root in seen:
            continue
        unique.append(root)
        seen.add(root)

    return sorted(unique, key=lambda root: (-len(root.parts), str(root)))


def _match_test_root(source_file: str, *, test_roots: Sequence[Path]) -> tuple[Path, Path]:
    source_path = Path(source_file).resolve()
    for root in test_roots:
        try:
            return root, source_path.relative_to(root)
        except ValueError:
            continue
    raise ValueError(f"Could not match test source {source_file!r} to any configured test root.")


def _generated_test_relpath_from_source(rel_source: Path, *, generated_dir: str) -> Path:
    if rel_source.name == "__init__.py":
        suffix = (
            rel_source.parent / "__init__.py" if rel_source.parent.parts else Path("__init__.py")
        )
        return Path(generated_dir) / suffix
    return Path(generated_dir) / rel_source


def _resolve_test_output_path(
    *,
    project_dir: Path,
    source_file: str,
    generated_dir: str,
    tests_package: str,
    test_roots: Sequence[Path] | None,
) -> Path:
    roots = _resolve_test_roots(
        project_dir=project_dir,
        tests_package=tests_package,
        test_roots=test_roots,
    )
    matched_root, rel_source = _match_test_root(source_file, test_roots=roots)
    return matched_root / _generated_test_relpath_from_source(
        rel_source,
        generated_dir=generated_dir,
    )


def _ensure_init_files(project_dir: Path, relpath: Path) -> None:
    parts = list(relpath.parts)
    if not parts:
        return
    dir_parts = parts[:-1]
    for i in range(1, len(dir_parts) + 1):
        d = project_dir / Path(*dir_parts[:i])
        d.mkdir(parents=True, exist_ok=True)
        init = d / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")


def _write_generated_test_module(
    *,
    project_dir: Path,
    generated_dir: str,
    source: str,
    header_fields: dict[str, object],
    out_path: Path | None = None,
    tests_package: str | None = None,
    module_name: str | None = None,
) -> Path:
    if out_path is None:
        if not tests_package or not module_name:
            raise TypeError(
                "_write_generated_test_module() requires either out_path or "
                "tests_package + module_name"
            )
        if module_name.split(".", 1)[0] != tests_package:
            raise ValueError(f"Test module {module_name!r} is not under {tests_package!r}.")
        gen_mod = paths.spec_module_to_generated_module(module_name, generated_dir=generated_dir)
        rel = paths.generated_module_to_relpath(gen_mod, generated_dir=generated_dir)
        parts = rel.parts
        if len(parts) < 2 or parts[0] != tests_package or parts[1] != generated_dir:
            raise ValueError(f"Refusing to write outside {tests_package}/{generated_dir}: {rel!s}")
        out_path = project_dir / rel

    out_path = out_path.resolve()

    root = project_dir.resolve()
    if root not in out_path.parents and out_path != root:
        raise ValueError("Refusing to write outside project_dir.")

    relpath = out_path.relative_to(root)
    if generated_dir not in relpath.parts:
        raise ValueError("Refusing to write outside tests generated dir.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_init_files(project_dir, relpath)

    # Place AGENTS.md (+ CLAUDE.md symlink) in the __generated__/ root so
    # coding agents know not to touch the contents.
    for parent in out_path.parents:
        if parent.name == generated_dir:
            ensure_agent_docs(parent)
            break

    hdr = format_header(**header_fields)  # type: ignore[arg-type]
    content = hdr + "\n" + (source or "").rstrip() + "\n"

    fd, tmp = tempfile.mkstemp(
        dir=str(out_path.parent),
        prefix=".jaunt-tmp-",
        suffix=".py",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out_path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return out_path


def detect_stale_test_modules(
    *,
    project_dir: Path,
    generated_dir: str,
    module_specs: dict[str, list[SpecEntry]],
    specs: dict[SpecRef, SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
    generation_fingerprint: str = "",
    module_context_digests: dict[str, str] | None = None,
    tests_package: str = "tests",
    test_roots: Sequence[Path] | None = None,
    force: bool = False,
) -> set[str]:
    if force:
        return set(module_specs.keys())

    stale: set[str] = set()
    for module_name, entries in module_specs.items():
        if not entries:
            stale.add(module_name)
            continue

        try:
            out_path = _resolve_test_output_path(
                project_dir=project_dir,
                source_file=entries[0].source_file,
                generated_dir=generated_dir,
                tests_package=tests_package,
                test_roots=test_roots,
            )
        except Exception:
            stale.add(module_name)
            continue

        if not out_path.exists():
            stale.add(module_name)
            continue

        try:
            existing = out_path.read_text(encoding="utf-8")
        except Exception:
            stale.add(module_name)
            continue

        on_disk = _normalize_digest(extract_module_digest(existing))
        computed = _normalize_digest(module_digest(module_name, entries, specs, spec_graph))
        if on_disk is None or computed is None or on_disk != computed:
            stale.add(module_name)
            continue
        if generation_fingerprint:
            on_disk_generation = _normalize_digest(extract_generation_fingerprint(existing))
            computed_generation = _normalize_digest(generation_fingerprint)
            if (
                on_disk_generation is None
                or computed_generation is None
                or on_disk_generation != computed_generation
            ):
                stale.add(module_name)
                continue
        if module_context_digests is not None:
            on_disk_context = _normalize_digest(extract_module_context_digest(existing))
            computed_context = _normalize_digest(module_context_digests.get(module_name))
            if (
                on_disk_context is None
                or computed_context is None
                or on_disk_context != computed_context
            ):
                stale.add(module_name)

    return stale


def _collect_existing_generated_test_files(
    *,
    project_dir: Path,
    tests_package: str,
    generated_dir: str,
    module_specs: dict[str, list[SpecEntry]],
    test_roots: Sequence[Path] | None,
) -> list[Path]:
    found: set[Path] = set()
    for entries in module_specs.values():
        if not entries:
            continue
        try:
            out_path = _resolve_test_output_path(
                project_dir=project_dir,
                source_file=entries[0].source_file,
                generated_dir=generated_dir,
                tests_package=tests_package,
                test_roots=test_roots,
            )
        except Exception:
            continue
        if out_path.exists():
            found.add(out_path)
    return sorted(found, key=lambda path: str(path))


def _collect_generated_test_paths_by_module(
    *,
    project_dir: Path,
    tests_package: str,
    generated_dir: str,
    module_specs: dict[str, list[SpecEntry]],
    test_roots: Sequence[Path] | None,
) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for module_name, entries in module_specs.items():
        if not entries:
            continue
        try:
            out[module_name] = _resolve_test_output_path(
                project_dir=project_dir,
                source_file=entries[0].source_file,
                generated_dir=generated_dir,
                tests_package=tests_package,
                test_roots=test_roots,
            )
        except Exception:
            continue
    return out


def _collect_generated_build_paths_by_module(
    *,
    package_dir: Path,
    generated_dir: str,
    module_specs: dict[str, list[SpecEntry]],
) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for module_name in module_specs:
        gen_mod = paths.spec_module_to_generated_module(module_name, generated_dir=generated_dir)
        rel = paths.generated_module_to_relpath(gen_mod, generated_dir=generated_dir)
        out[module_name] = (package_dir / rel).resolve()
    return out


@dataclass(frozen=True, slots=True)
class TestGenerationReport:
    generated: set[str]
    skipped: set[str]
    failed: dict[str, list[str]]
    generated_files: list[Path]


@dataclass(frozen=True, slots=True)
class RepairBuildContext:
    package_dir: Path
    generated_dir: str
    module_specs: dict[str, list[SpecEntry]]
    specs: dict[SpecRef, SpecEntry]
    spec_graph: dict[SpecRef, set[SpecRef]]
    module_dag: dict[str, set[str]]
    backend: GeneratorBackend
    generation_fingerprint: str
    skills_block: str = ""
    jobs: int = 1
    async_runner: str = "asyncio"


@dataclass(frozen=True, slots=True)
class PytestResult:
    exit_code: int
    passed: bool
    failed: bool
    failures: list[str]
    generation_failed: dict[str, list[str]] = field(default_factory=dict)


def _compact_failure_context(stdout: str, stderr: str, *, max_lines: int = 28) -> list[str]:
    lines = [line.rstrip() for line in f"{stdout}\n{stderr}".splitlines() if line.strip()]
    if len(lines) <= max_lines:
        return lines
    head = lines[: max_lines // 2]
    tail = lines[-(max_lines // 2) :]
    return [*head, "... pytest failure context truncated ...", *tail]


def _path_mentions(text: str, path: Path, *, cwd: Path | None) -> bool:
    haystack = text.replace("\\", "/")
    candidates = {str(path.resolve()).replace("\\", "/"), path.as_posix()}
    if cwd is not None:
        try:
            rel = path.resolve().relative_to(cwd.resolve())
            candidates.add(rel.as_posix())
        except ValueError:
            pass
    return any(candidate in haystack for candidate in candidates)


def _implicated_modules_from_pytest(
    *,
    stdout: str,
    stderr: str,
    test_paths_by_module: dict[str, Path],
    build_paths_by_module: dict[str, Path],
    cwd: Path | None,
) -> tuple[set[str], set[str]]:
    text = f"{stdout}\n{stderr}"
    failed_tests = {
        module_name
        for module_name, path in test_paths_by_module.items()
        if _path_mentions(text, path, cwd=cwd)
    }
    implicated_build = {
        module_name
        for module_name, path in build_paths_by_module.items()
        if _path_mentions(text, path, cwd=cwd)
    }
    return failed_tests, implicated_build


def _critical_path_lengths(modules: set[str], dag: dict[str, set[str]]) -> dict[str, int]:
    dependents: dict[str, set[str]] = {m: set() for m in modules}
    for m in modules:
        for dep in dag.get(m, set()):
            if dep in modules:
                dependents.setdefault(dep, set()).add(m)

    memo: dict[str, int] = {}

    def length(m: str) -> int:
        if m in memo:
            return memo[m]
        kids = dependents.get(m, set())
        if not kids:
            memo[m] = 0
            return 0
        v = 1 + max(length(k) for k in kids)
        memo[m] = v
        return v

    for m in modules:
        length(m)
    return memo


async def run_test_generation(
    *,
    project_dir: Path,
    tests_package: str,
    generated_dir: str,
    test_roots: Sequence[Path] | None = None,
    dependency_apis: dict[SpecRef, str] | None = None,
    module_specs: dict[str, list[SpecEntry]],
    specs: dict[SpecRef, SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
    module_dag: dict[str, set[str]],
    stale_modules: set[str],
    backend: GeneratorBackend,
    generation_fingerprint: str = "",
    jobs: int = 4,
    progress: object | None = None,
    response_cache: ResponseCache | None = None,
    cost_tracker: CostTracker | None = None,
    async_runner: str = "asyncio",
    initial_error_context_by_module: dict[str, list[str]] | None = None,
) -> TestGenerationReport:
    jobs = max(1, int(jobs))

    stale = set(stale_modules) & set(module_specs.keys())
    skipped = set(module_specs.keys()) - stale
    if not stale:
        return TestGenerationReport(
            generated=set(),
            skipped=skipped,
            failed={},
            generated_files=[],
        )

    deps_in_stale: dict[str, set[str]] = {}
    dependents: dict[str, set[str]] = {m: set() for m in stale}
    indeg: dict[str, int] = {m: 0 for m in stale}

    for m in stale:
        deps = {d for d in module_dag.get(m, set()) if d in stale}
        deps_in_stale[m] = deps
        indeg[m] = len(deps)
        for d in deps:
            dependents.setdefault(d, set()).add(m)

    from jaunt.deps import toposort

    toposort(deps_in_stale)

    prio = _critical_path_lengths(stale, module_dag)
    ready: list[tuple[int, str]] = []
    for m, n in indeg.items():
        if n == 0:
            heapq.heappush(ready, (-prio.get(m, 0), m))

    generated: set[str] = set()
    failed: dict[str, list[str]] = {}
    generated_files: list[Path] = []
    completed: set[str] = set()

    async def gen_one(module_name: str) -> tuple[bool, list[str], Path | None]:
        entries = module_specs.get(module_name, [])
        if not entries:
            return False, ["No test specs found for module."], None
        expected, conflict_errs = _build_expected_names(entries)
        if conflict_errs:
            return False, conflict_errs, None

        spec_sources: dict[SpecRef, str] = {}
        decorator_prompts: dict[SpecRef, str] = {}
        public_api_only_by_name = test_public_api_only_by_name(entries)
        for e in entries:
            spec_source = extract_source_segment(e)
            if not public_api_only_by_name.get(e.qualname, True):
                spec_source = (
                    f"{spec_source.rstrip()}\n\n"
                    "# Test policy note\n"
                    "White-box testing is explicitly allowed for this spec. "
                    "Implementation details may be inspected when needed.\n"
                )
            spec_sources[e.spec_ref] = spec_source
            p = e.decorator_kwargs.get("prompt")
            if isinstance(p, str) and p:
                decorator_prompts[e.spec_ref] = p
        module_contract = build_module_contract(entries=entries, expected_names=expected)

        ctx = ModuleSpecContext(
            kind="test",
            spec_module=module_name,
            generated_module=paths.spec_module_to_generated_module(
                module_name, generated_dir=generated_dir
            ),
            expected_names=expected,
            spec_sources=spec_sources,
            decorator_prompts=decorator_prompts,
            dependency_apis=dependency_apis or {},
            dependency_generated_modules={},
            module_contract_block=module_contract.prompt_block,
            module_context_digest=module_contract.digest,
            async_runner=async_runner,
        )

        def _validate_candidate(source: str) -> list[str]:
            return validate_test_generated_source(
                source,
                expected,
                spec_module=ctx.spec_module,
                generated_module=ctx.generated_module,
                public_api_only_by_name=public_api_only_by_name,
            )

        def _retry_validator(source: str) -> list[str]:
            return validate_test_contract_only(
                source,
                spec_module=ctx.spec_module,
                generated_module=ctx.generated_module,
                public_api_only_by_name=public_api_only_by_name,
            )

        # Check response cache before calling LLM.
        result_source: str | None = None
        ck: str | None = None
        if response_cache is not None:
            ck = cache_key_from_context(
                ctx,
                model=backend.model_name,
                provider=backend.provider_name,
                generation_fingerprint=generation_fingerprint,
            )
            cached = response_cache.get(ck)
            if cached is not None:
                cache_errors = _validate_candidate(cached.source)
                if not cache_errors:
                    result_source = cached.source
                    if cost_tracker is not None:
                        cost_tracker.record_cache_hit()

        if result_source is None:
            result = await backend.generate_with_retry(
                ctx,
                extra_validator=_retry_validator,
                initial_error_context=(initial_error_context_by_module or {}).get(module_name),
            )
            if result.source is None:
                return False, result.errors or ["No source returned."], None

            errors = _validate_candidate(result.source)
            if errors:
                return False, errors, None

            result_source = result.source

            if cost_tracker is not None and result.usage is not None:
                cost_tracker.record(module_name, result.usage)

            if response_cache is not None and ck is not None:
                import time

                entry = CacheEntry(
                    source=result_source,
                    prompt_tokens=result.usage.prompt_tokens if result.usage else 0,
                    completion_tokens=result.usage.completion_tokens if result.usage else 0,
                    model=result.usage.model if result.usage else "",
                    provider=result.usage.provider if result.usage else "",
                    cached_at=time.time(),
                )
                response_cache.put(ck, entry)

        digest = module_digest(module_name, entries, specs, spec_graph)
        header_fields = {
            "tool_version": _tool_version(),
            "kind": "test",
            "source_module": module_name,
            "module_digest": digest,
            "generation_fingerprint": generation_fingerprint,
            "module_context_digest": module_contract.digest,
            "spec_refs": [str(e.spec_ref) for e in entries],
        }

        out_path = _resolve_test_output_path(
            project_dir=project_dir,
            source_file=entries[0].source_file,
            generated_dir=generated_dir,
            tests_package=tests_package,
            test_roots=test_roots,
        )
        out = _write_generated_test_module(
            project_dir=project_dir,
            out_path=out_path,
            generated_dir=generated_dir,
            source=result_source,
            header_fields=header_fields,
        )
        return True, [], out

    async def complete(m: str) -> None:
        for dep in sorted(dependents.get(m, set())):
            if dep in completed:
                continue
            indeg[dep] -= 1
            if indeg[dep] != 0:
                continue

            bad = [d for d in deps_in_stale.get(dep, set()) if d in failed]
            if bad:
                failed[dep] = [f"Dependency failed: {d}" for d in bad]
                completed.add(dep)
                if progress is not None:
                    try:
                        progress.advance(dep, ok=False)  # type: ignore[attr-defined]
                    except Exception:
                        pass
                await complete(dep)
            else:
                heapq.heappush(ready, (-prio.get(dep, 0), dep))

    in_flight: dict[asyncio.Task[tuple[bool, list[str], Path | None]], str] = {}

    while ready or in_flight:
        while ready and len(in_flight) < jobs:
            _, m = heapq.heappop(ready)
            if m in completed:
                continue
            t: asyncio.Task[tuple[bool, list[str], Path | None]] = asyncio.create_task(gen_one(m))
            in_flight[t] = m

        if not in_flight:
            break

        done, _ = await asyncio.wait(in_flight.keys(), return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            m = in_flight.pop(t)
            ok = False
            errs: list[str] = []
            out: Path | None = None
            try:
                ok, errs, out = t.result()
            except Exception as e:  # pragma: no cover
                ok = False
                errs = [f"Unhandled error: {e!r}"]

            completed.add(m)
            if ok:
                generated.add(m)
                if out is not None:
                    generated_files.append(out)
            else:
                failed[m] = errs or ["Unknown error."]

            if progress is not None:
                try:
                    progress.advance(m, ok=ok)  # type: ignore[attr-defined]
                except Exception:
                    pass

            await complete(m)

        # Check budget after processing completed tasks.
        if cost_tracker is not None:
            try:
                cost_tracker.check_budget()
            except JauntGenerationError:
                for t in in_flight:
                    t.cancel()
                for rem in stale - completed:
                    failed[rem] = ["Budget limit exceeded."]
                    completed.add(rem)
                in_flight.clear()
                break

    if progress is not None:
        try:
            progress.finish()  # type: ignore[attr-defined]
        except Exception:
            pass

    remaining = stale - completed
    if remaining:
        sub = {m: {d for d in deps_in_stale.get(m, set()) if d in remaining} for m in remaining}
        toposort(sub)
        raise JauntGenerationError("Test generation scheduler deadlock.")

    return TestGenerationReport(
        generated=generated,
        skipped=skipped,
        failed=failed,
        generated_files=sorted(generated_files, key=lambda p: str(p)),
    )


async def run_tests(
    *,
    project_dir: Path,
    tests_package: str = "tests",
    generated_dir: str = "__generated__",
    test_roots: Sequence[Path] | None = None,
    dependency_apis: dict[SpecRef, str] | None = None,
    module_specs: dict[str, list[SpecEntry]] | None = None,
    specs: dict[SpecRef, SpecEntry] | None = None,
    spec_graph: dict[SpecRef, set[SpecRef]] | None = None,
    module_dag: dict[str, set[str]] | None = None,
    stale_modules: set[str] | None = None,
    backend: GeneratorBackend | None = None,
    generation_fingerprint: str = "",
    jobs: int = 4,
    pytest_args: list[str] | None = None,
    no_generate: bool = False,
    no_run: bool = False,
    progress: object | None = None,
    pythonpath: Sequence[Path] | None = None,
    cwd: Path | None = None,
    response_cache: ResponseCache | None = None,
    cost_tracker: CostTracker | None = None,
    async_runner: str = "asyncio",
    repair_build_context: RepairBuildContext | None = None,
) -> PytestResult:
    generated_files: list[Path] = []
    gen_failed: dict[str, list[str]] = {}

    if not no_generate:
        if (
            module_specs is None
            or specs is None
            or spec_graph is None
            or module_dag is None
            or stale_modules is None
            or backend is None
        ):
            raise ValueError(
                "Missing generation inputs (module_specs/specs/spec_graph/module_dag)."
            )

        report = await run_test_generation(
            project_dir=project_dir,
            tests_package=tests_package,
            generated_dir=generated_dir,
            test_roots=test_roots,
            dependency_apis=dependency_apis,
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag=module_dag,
            stale_modules=stale_modules,
            backend=backend,
            generation_fingerprint=generation_fingerprint,
            jobs=jobs,
            progress=progress,
            response_cache=response_cache,
            cost_tracker=cost_tracker,
            async_runner=async_runner,
        )
        generated_files = report.generated_files
        gen_failed = report.failed
    elif module_specs is not None:
        generated_files = _collect_existing_generated_test_files(
            project_dir=project_dir,
            tests_package=tests_package,
            generated_dir=generated_dir,
            module_specs=module_specs,
            test_roots=test_roots,
        )

    if no_run:
        exit_code = 3 if gen_failed else 0
        return PytestResult(
            exit_code=exit_code,
            passed=exit_code == 0,
            failed=exit_code != 0,
            failures=[],
            generation_failed=gen_failed,
        )

    pytest_result = _run_pytest_capture(
        generated_files,
        pytest_args=pytest_args,
        pythonpath=pythonpath,
        cwd=cwd,
    )
    pytest_exit_code = pytest_result.exit_code

    if (
        pytest_exit_code != 0
        and not gen_failed
        and not no_generate
        and module_specs is not None
        and specs is not None
        and spec_graph is not None
        and module_dag is not None
        and backend is not None
    ):
        repair_lines = _compact_failure_context(pytest_result.stdout, pytest_result.stderr)
        test_paths_by_module = _collect_generated_test_paths_by_module(
            project_dir=project_dir,
            tests_package=tests_package,
            generated_dir=generated_dir,
            module_specs=module_specs,
            test_roots=test_roots,
        )
        build_paths_by_module: dict[str, Path] = {}
        if repair_build_context is not None:
            build_paths_by_module = _collect_generated_build_paths_by_module(
                package_dir=repair_build_context.package_dir,
                generated_dir=repair_build_context.generated_dir,
                module_specs=repair_build_context.module_specs,
            )

        failed_test_modules, implicated_build_modules = _implicated_modules_from_pytest(
            stdout=pytest_result.stdout,
            stderr=pytest_result.stderr,
            test_paths_by_module=test_paths_by_module,
            build_paths_by_module=build_paths_by_module,
            cwd=cwd,
        )

        if implicated_build_modules and repair_build_context is not None:
            from jaunt import builder

            repaired_build = await builder.run_build(
                package_dir=repair_build_context.package_dir,
                generated_dir=repair_build_context.generated_dir,
                module_specs=repair_build_context.module_specs,
                specs=repair_build_context.specs,
                spec_graph=repair_build_context.spec_graph,
                module_dag=repair_build_context.module_dag,
                stale_modules=builder.expand_stale_modules(
                    repair_build_context.module_dag,
                    implicated_build_modules,
                ),
                backend=repair_build_context.backend,
                generation_fingerprint=repair_build_context.generation_fingerprint,
                skills_block=repair_build_context.skills_block,
                jobs=repair_build_context.jobs,
                progress=None,
                response_cache=None,
                cost_tracker=cost_tracker,
                async_runner=repair_build_context.async_runner,
                initial_error_context_by_module={
                    module_name: repair_lines for module_name in implicated_build_modules
                },
            )
            if repaired_build.failed:
                return PytestResult(
                    exit_code=3,
                    passed=False,
                    failed=True,
                    failures=repair_lines,
                    generation_failed=repaired_build.failed,
                )

        if failed_test_modules:
            repaired_tests = await run_test_generation(
                project_dir=project_dir,
                tests_package=tests_package,
                generated_dir=generated_dir,
                test_roots=test_roots,
                dependency_apis=dependency_apis,
                module_specs=module_specs,
                specs=specs,
                spec_graph=spec_graph,
                module_dag=module_dag,
                stale_modules=failed_test_modules,
                backend=backend,
                generation_fingerprint=generation_fingerprint,
                jobs=jobs,
                progress=None,
                response_cache=None,
                cost_tracker=cost_tracker,
                async_runner=async_runner,
                initial_error_context_by_module={
                    module_name: repair_lines for module_name in failed_test_modules
                },
            )
            if repaired_tests.failed:
                return PytestResult(
                    exit_code=3,
                    passed=False,
                    failed=True,
                    failures=repair_lines,
                    generation_failed=repaired_tests.failed,
                )

        if implicated_build_modules or failed_test_modules:
            generated_files = _collect_existing_generated_test_files(
                project_dir=project_dir,
                tests_package=tests_package,
                generated_dir=generated_dir,
                module_specs=module_specs,
                test_roots=test_roots,
            )
            pytest_result = _run_pytest_capture(
                generated_files,
                pytest_args=pytest_args,
                pythonpath=pythonpath,
                cwd=cwd,
            )
            pytest_exit_code = pytest_result.exit_code

    exit_code = 3 if gen_failed else pytest_exit_code
    return PytestResult(
        exit_code=exit_code,
        passed=exit_code == 0,
        failed=bool(gen_failed) or pytest_exit_code != 0,
        failures=_compact_failure_context(pytest_result.stdout, pytest_result.stderr)
        if pytest_exit_code != 0
        else [],
        generation_failed=gen_failed,
    )
