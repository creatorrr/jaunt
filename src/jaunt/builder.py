from __future__ import annotations

import asyncio
import heapq
import importlib.metadata
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from jaunt import paths
from jaunt.digest import extract_source_segment, module_digest
from jaunt.errors import JauntDependencyCycleError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.header import extract_module_digest, format_header
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef
from jaunt.validation import validate_generated_source


def _tool_version() -> str:
    try:
        return importlib.metadata.version("jaunt")
    except Exception:
        return "0"


def _normalize_digest(digest: str | None) -> str | None:
    if not digest:
        return None
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1]
    return digest


def _generated_relpath(module_name: str, *, generated_dir: str) -> Path:
    generated_module = paths.spec_module_to_generated_module(
        module_name, generated_dir=generated_dir
    )
    return paths.generated_module_to_relpath(generated_module, generated_dir=generated_dir)


def _ensure_init_files(package_dir: Path, relpath: Path) -> None:
    # Ensure all parent package dirs contain __init__.py so imports work.
    parts = list(relpath.parts)
    if not parts:
        return
    dir_parts = parts[:-1]
    for i in range(1, len(dir_parts) + 1):
        d = package_dir / Path(*dir_parts[:i])
        d.mkdir(parents=True, exist_ok=True)
        init = d / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")


def write_generated_module(
    *,
    package_dir: Path,
    generated_dir: str,
    module_name: str,
    source: str,
    header_fields: dict[str, object],
) -> Path:
    """Atomically write a generated module file with a Jaunt header."""

    relpath = _generated_relpath(module_name, generated_dir=generated_dir)
    out_path = (package_dir / relpath).resolve()
    root = package_dir.resolve()
    if root not in out_path.parents and out_path != root:
        raise ValueError("Refusing to write outside package_dir.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_init_files(package_dir, relpath)

    hdr = format_header(**header_fields)  # type: ignore[arg-type]
    content = hdr + "\n" + (source or "").rstrip() + "\n"

    # Write atomically: temp file in the same directory then os.replace.
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


def detect_stale_modules(
    *,
    package_dir: Path,
    generated_dir: str,
    module_specs: dict[str, list[SpecEntry]],
    specs: dict[SpecRef, SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
    force: bool = False,
) -> set[str]:
    if force:
        return set(module_specs.keys())

    stale: set[str] = set()
    for module_name, entries in module_specs.items():
        relpath = _generated_relpath(module_name, generated_dir=generated_dir)
        out_path = package_dir / relpath
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

    return stale


def expand_stale_modules(module_dag: dict[str, set[str]], stale_modules: set[str]) -> set[str]:
    """If a module is stale, all its dependents are stale (transitively)."""

    dependents: dict[str, set[str]] = {}
    for mod, deps in module_dag.items():
        for dep in deps:
            dependents.setdefault(dep, set()).add(mod)

    expanded = set(stale_modules)
    queue = list(stale_modules)
    while queue:
        m = queue.pop()
        for dep in dependents.get(m, set()):
            if dep in expanded:
                continue
            expanded.add(dep)
            queue.append(dep)
    return expanded


@dataclass(frozen=True, slots=True)
class BuildReport:
    generated: set[str]
    skipped: set[str]
    failed: dict[str, list[str]]


def _critical_path_lengths(modules: set[str], dag: dict[str, set[str]]) -> dict[str, int]:
    # Priority heuristic: prefer nodes with the longest remaining downstream path length.
    dep_to_dependents: dict[str, set[str]] = {m: set() for m in modules}
    for m in modules:
        for dep in dag.get(m, set()):
            if dep in modules:
                dep_to_dependents.setdefault(dep, set()).add(m)

    memo: dict[str, int] = {}

    def length(m: str) -> int:
        if m in memo:
            return memo[m]
        children = dep_to_dependents.get(m, set())
        if not children:
            memo[m] = 0
            return 0
        v = 1 + max(length(c) for c in children)
        memo[m] = v
        return v

    for m in modules:
        length(m)
    return memo


def _raise_cycle_error(module_graph: dict[str, set[str]]) -> None:
    # Delegate cycle extraction/formatting to deps.toposort, which raises
    # JauntDependencyCycleError with the participants in the message.
    from jaunt.deps import toposort

    try:
        toposort(module_graph)
    except JauntDependencyCycleError:
        raise
    raise JauntDependencyCycleError("Dependency cycle detected.")


def _assert_acyclic(module_graph: dict[str, set[str]]) -> None:
    from jaunt.deps import toposort

    # `toposort` raises JauntDependencyCycleError and includes participants.
    toposort(module_graph)


async def run_build(
    *,
    package_dir: Path,
    generated_dir: str,
    module_specs: dict[str, list[SpecEntry]],
    specs: dict[SpecRef, SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
    module_dag: dict[str, set[str]],
    stale_modules: set[str],
    backend: GeneratorBackend,
    skills_block: str = "",
    jobs: int = 4,
    progress: object | None = None,
) -> BuildReport:
    jobs = max(1, int(jobs))

    # Expand rebuild set and restrict to modules we actually have specs for.
    expanded = expand_stale_modules(module_dag, set(stale_modules))
    stale = expanded & set(module_specs.keys())
    skipped = set(module_specs.keys()) - stale

    if not stale:
        return BuildReport(generated=set(), skipped=skipped, failed={})

    # Induce a subgraph over stale modules.
    deps_in_stale: dict[str, set[str]] = {}
    dependents: dict[str, set[str]] = {m: set() for m in stale}
    indeg: dict[str, int] = {m: 0 for m in stale}

    for m in stale:
        deps = {d for d in module_dag.get(m, set()) if d in stale}
        deps_in_stale[m] = deps
        indeg[m] = len(deps)
        for d in deps:
            dependents.setdefault(d, set()).add(m)

    _assert_acyclic(deps_in_stale)

    prio = _critical_path_lengths(stale, module_dag)

    ready: list[tuple[int, str]] = []
    for m, n in indeg.items():
        if n == 0:
            heapq.heappush(ready, (-prio.get(m, 0), m))

    generated: set[str] = set()
    generated_sources: dict[str, str] = {}  # module_name -> generated source
    failed: dict[str, list[str]] = {}
    completed: set[str] = set()

    # Track generated source for dependency context injection.
    generated_sources: dict[str, str] = {}

    def _collect_dependency_context(
        module_name: str,
    ) -> tuple[dict[SpecRef, str], dict[str, str]]:
        """Collect API signatures and generated source from dependency modules."""
        dep_apis: dict[SpecRef, str] = {}
        dep_gen: dict[str, str] = {}

        dep_modules = module_dag.get(module_name, set())
        for dep_mod in dep_modules:
            # Collect spec API signatures from dependency modules.
            for dep_entry in module_specs.get(dep_mod, []):
                try:
                    dep_apis[dep_entry.spec_ref] = extract_source_segment(dep_entry)
                except Exception:
                    pass

            # Collect already-generated source (from this build or pre-existing).
            if dep_mod in generated_sources:
                dep_gen[dep_mod] = generated_sources[dep_mod]
            else:
                # Try reading from disk (pre-existing generated file).
                relpath = _generated_relpath(dep_mod, generated_dir=generated_dir)
                gen_path = package_dir / relpath
                try:
                    if gen_path.exists():
                        dep_gen[dep_mod] = gen_path.read_text(encoding="utf-8")
                except Exception:
                    pass

        return dep_apis, dep_gen

    async def build_one(module_name: str) -> tuple[bool, list[str]]:
        entries = module_specs.get(module_name, [])
        expected = [e.qualname for e in entries]

        spec_sources: dict[SpecRef, str] = {}
        decorator_prompts: dict[SpecRef, str] = {}
        for e in entries:
            spec_sources[e.spec_ref] = extract_source_segment(e)
            p = e.decorator_kwargs.get("prompt")
            if isinstance(p, str) and p:
                decorator_prompts[e.spec_ref] = p

        dep_apis, dep_gen = _collect_dependency_context(module_name)

        ctx = ModuleSpecContext(
            kind="build",
            spec_module=module_name,
            generated_module=paths.spec_module_to_generated_module(
                module_name, generated_dir=generated_dir
            ),
            expected_names=expected,
            spec_sources=spec_sources,
            decorator_prompts=decorator_prompts,
            dependency_apis=dep_apis,
            dependency_generated_modules=dep_gen,
            skills_block=skills_block,
        )

        result = await backend.generate_with_retry(ctx)
        if result.source is None:
            return False, result.errors or ["No source returned."]

        errors = validate_generated_source(result.source, expected)
        if errors:
            return False, errors

        # Store generated source for downstream dependents.
        generated_sources[module_name] = result.source

        digest = module_digest(module_name, entries, specs, spec_graph)
        header_fields = {
            "tool_version": _tool_version(),
            "kind": "build",
            "source_module": module_name,
            "module_digest": digest,
            "spec_refs": [str(e.spec_ref) for e in entries],
        }

        write_generated_module(
            package_dir=package_dir,
            generated_dir=generated_dir,
            module_name=module_name,
            source=result.source,
            header_fields=header_fields,
        )
        # Store generated source so downstream modules get it as context.
        generated_sources[module_name] = result.source
        return True, []

    async def complete(m: str) -> None:
        # Decrement indegrees of dependents and enqueue when ready.
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

    in_flight: dict[asyncio.Task[tuple[bool, list[str]]], str] = {}

    while ready or in_flight:
        while ready and len(in_flight) < jobs:
            _, m = heapq.heappop(ready)
            if m in completed:
                continue
            t: asyncio.Task[tuple[bool, list[str]]] = asyncio.create_task(build_one(m))
            in_flight[t] = m

        if not in_flight:
            break

        done, _ = await asyncio.wait(in_flight.keys(), return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            m = in_flight.pop(t)
            ok = False
            errs: list[str] = []
            try:
                ok, errs = t.result()
            except Exception as e:  # pragma: no cover - defensive.
                ok = False
                errs = [f"Unhandled error: {e!r}"]

            completed.add(m)
            if ok:
                generated.add(m)
            else:
                failed[m] = errs or ["Unknown error."]

            if progress is not None:
                try:
                    progress.advance(m, ok=ok)  # type: ignore[attr-defined]
                except Exception:
                    pass

            await complete(m)

    remaining = stale - completed
    if remaining:
        # Scheduler deadlock: remaining modules could not become ready. Most
        # likely a dependency cycle among the remaining induced subgraph.
        sub = {m: {d for d in deps_in_stale.get(m, set()) if d in remaining} for m in remaining}
        _raise_cycle_error(sub)

    if progress is not None:
        try:
            progress.finish()  # type: ignore[attr-defined]
        except Exception:
            pass

    return BuildReport(generated=generated, skipped=skipped, failed=failed)
