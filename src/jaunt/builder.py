"""The forge: build orchestration and parallel scheduling.

What the hammer? what the chain? -- specs enter the furnace, implementations
emerge on the other side.
"""

from __future__ import annotations

import asyncio
import ast
import heapq
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from jaunt import paths
from jaunt.agent_docs import ensure_agent_docs
from jaunt.cache import CacheEntry, ResponseCache, cache_key_from_context
from jaunt.cost import CostTracker
from jaunt.digest import extract_source_segment, module_digest
from jaunt.errors import JauntDependencyCycleError, JauntGenerationError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.header import (
    extract_generation_fingerprint,
    extract_module_api_digest,
    extract_module_context_digest,
    extract_module_digest,
    format_header,
)
from jaunt.module_api import build_dependency_api_block, module_api_digest
from jaunt.module_contract import build_module_contract
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef
from jaunt.validation import validate_build_contract_only, validate_build_generated_source

_TY_CHECK_TIMEOUT_S = 20.0


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

    # Place AGENTS.md (+ CLAUDE.md symlink) in the __generated__/ root so
    # coding agents know not to touch the contents.
    for parent in out_path.parents:
        if parent.name == generated_dir:
            ensure_agent_docs(parent)
            break

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
    generation_fingerprint: str = "",
    module_context_digests: dict[str, str] | None = None,
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


def detect_api_changed_modules(
    *,
    package_dir: Path,
    generated_dir: str,
    module_specs: dict[str, list[SpecEntry]],
    module_api_digests: dict[str, str],
) -> set[str]:
    changed: set[str] = set()
    for module_name, entries in module_specs.items():
        if not entries:
            continue
        relpath = _generated_relpath(module_name, generated_dir=generated_dir)
        out_path = package_dir / relpath
        if not out_path.exists():
            changed.add(module_name)
            continue

        try:
            existing = out_path.read_text(encoding="utf-8")
        except Exception:
            changed.add(module_name)
            continue

        on_disk = _normalize_digest(extract_module_api_digest(existing))
        computed = _normalize_digest(module_api_digests.get(module_name))
        if on_disk is None or computed is None or on_disk != computed:
            changed.add(module_name)
    return changed


def expand_stale_modules(
    module_dag: dict[str, set[str]],
    stale_modules: set[str],
    *,
    changed_modules: set[str] | None = None,
) -> set[str]:
    """If a module's exported API changed, its dependents are stale transitively."""

    dependents: dict[str, set[str]] = {}
    for mod, deps in module_dag.items():
        for dep in deps:
            dependents.setdefault(dep, set()).add(mod)

    expanded = set(stale_modules)
    queue = list(changed_modules if changed_modules is not None else stale_modules)
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


@dataclass(frozen=True, slots=True)
class _GeneratedComponent:
    expected_names: tuple[str, ...]
    source: str


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


def _resolve_ty_cmd() -> list[str] | None:
    if shutil.which("ty"):
        return ["ty"]

    try:
        import ty  # noqa: F401

        return [sys.executable, "-m", "ty"]
    except Exception:
        return None


def _ty_error_context(
    *,
    source: str,
    module_name: str,
    package_dir: Path,
    generated_dir: str,
    ty_cmd: list[str],
) -> list[str]:
    relpath = _generated_relpath(module_name, generated_dir=generated_dir)
    with tempfile.TemporaryDirectory(prefix=".jaunt-ty-") as tmp:
        tmp_root = Path(tmp)
        tmp_path = tmp_root / relpath
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_init_files(tmp_root, relpath)
        tmp_path.write_text((source or "").rstrip() + "\n", encoding="utf-8")

        env = os.environ.copy()
        cur = env.get("PYTHONPATH") or ""
        cur_parts = [x for x in cur.split(os.pathsep) if x] if cur else []
        pp = [str(tmp_root.resolve()), str(package_dir.resolve()), *cur_parts]
        merged: list[str] = []
        seen: set[str] = set()
        for p in pp:
            if p in seen:
                continue
            merged.append(p)
            seen.add(p)
        env["PYTHONPATH"] = os.pathsep.join(merged)

        try:
            # NOTE: This is called from the async build flow through a sync
            # validator callback; keep it short and bounded.
            proc = subprocess.run(
                [*ty_cmd, "check", str(tmp_path)],
                cwd=str(package_dir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=_TY_CHECK_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            timeout_msg = f"ty check timed out for {module_name} after {_TY_CHECK_TIMEOUT_S:.1f}s."
            stderr_obj = exc.stderr
            if isinstance(stderr_obj, bytes):
                stderr = stderr_obj.decode("utf-8", errors="replace").strip()
            else:
                stderr = (stderr_obj or "").strip()
            if stderr:
                timeout_msg = f"{stderr}\n{timeout_msg}"
            return [timeout_msg]
        if proc.returncode == 0:
            return []

        raw = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if not raw:
            raw = f"ty check exited with status {proc.returncode}"
        error_codes = set(re.findall(r"error\[([^\]]+)\]", raw))
        if error_codes and error_codes.issubset({"unresolved-import"}):
            # The candidate source is checked from an isolated temp tree; imports
            # that resolve in the final project layout may be transiently
            # unresolved here. Ignore pure unresolved-import diagnostics.
            return []
        lines = [line for line in raw.splitlines() if line.strip()]
        snippet = "\n".join(lines[:16])
        return [f"ty check failed for {module_name}: {snippet}"]


def _build_expected_names(entries: list[SpecEntry]) -> tuple[list[str], list[str]]:
    """Compute expected top-level names for generated module output.

    Method specs (``class_name is not None``) are grouped by their owning class
    so that ``expected_names`` contains the class name, not individual method
    qualnames.  Returns ``(expected_names, errors)`` — errors is non-empty when
    a module has both whole-class ``@magic`` and per-method ``@magic`` on the
    same class.
    """
    expected: list[str] = []
    seen_classes: set[str] = set()
    class_level_specs: set[str] = set()
    method_level_classes: set[str] = set()

    for e in entries:
        if e.class_name is not None:
            method_level_classes.add(e.class_name)
            if e.class_name not in seen_classes:
                expected.append(e.class_name)
                seen_classes.add(e.class_name)
        else:
            expected.append(e.qualname)
            # Track classes that have a whole-class @magic spec.
            if "." not in e.qualname:
                class_level_specs.add(e.qualname)

    # Detect conflict: whole-class @magic + per-method @magic on the same class.
    conflicts = class_level_specs & method_level_classes
    if conflicts:
        names = ", ".join(sorted(conflicts))
        return expected, [
            f"Conflicting @magic: class(es) {names} have both whole-class @magic "
            f"and per-method @magic decorators. Use one or the other."
        ]

    return expected, []


def _component_entries(
    *,
    module_name: str,
    entries: list[SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
) -> list[list[SpecEntry]]:
    by_ref = {entry.spec_ref: entry for entry in entries}
    refs = set(by_ref)
    if len(refs) <= 1:
        return [list(entries)] if entries else []

    adjacency: dict[SpecRef, set[SpecRef]] = {ref: set() for ref in refs}
    for ref in refs:
        for dep in spec_graph.get(ref, set()):
            if dep in refs:
                adjacency[ref].add(dep)
                if dep not in adjacency:
                    adjacency[dep] = set()
                adjacency[dep].add(ref)

    class_refs: dict[str, set[SpecRef]] = {}
    for entry in entries:
        if entry.class_name:
            class_refs.setdefault(entry.class_name, set()).add(entry.spec_ref)
    for refs_for_class in class_refs.values():
        ordered = _sorted_spec_refs(refs_for_class)
        for left in ordered:
            adjacency[left].update(ref for ref in ordered if ref != left)

    components: list[list[SpecEntry]] = []
    visited: set[SpecRef] = set()
    for ref in _sorted_spec_refs(refs):
        if ref in visited:
            continue
        stack: list[SpecRef] = [ref]
        bucket: list[SpecEntry] = []
        while stack:
            cur: SpecRef = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            bucket.append(by_ref[cur])
            for nxt in _sorted_spec_refs(adjacency[cur], reverse=True):
                if nxt not in visited:
                    stack.append(nxt)
        bucket.sort(key=lambda entry: (entry.qualname, str(entry.spec_ref)))
        components.append(bucket)

    components.sort(key=lambda bucket: (bucket[0].qualname, str(bucket[0].spec_ref)))
    return components


def _defined_top_level_names(node: ast.stmt) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        return {target.id for target in node.targets if isinstance(target, ast.Name)}
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    return set()


def _sorted_spec_refs(refs: set[SpecRef], *, reverse: bool = False) -> list[SpecRef]:
    return cast(list[SpecRef], sorted(refs, key=str, reverse=reverse))


def _merge_generated_components(components: list[_GeneratedComponent]) -> tuple[str, list[str]]:
    import_texts: list[str] = []
    seen_imports: set[str] = set()
    body_texts: list[str] = []
    seen_names: set[str] = set()

    for component in components:
        try:
            mod = ast.parse(component.source)
        except SyntaxError as exc:
            names = ", ".join(component.expected_names)
            return "", [f"Failed to parse generated component for {names}: {exc.msg}"]

        for node in mod.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                rendered = ast.unparse(node).strip()
                if rendered and rendered not in seen_imports:
                    seen_imports.add(rendered)
                    import_texts.append(rendered)
                continue

            names = _defined_top_level_names(node)
            dupes = names & seen_names
            if dupes:
                dupes_str = ", ".join(sorted(dupes))
                return "", [f"Component merge conflict: duplicate top-level name(s): {dupes_str}"]
            seen_names.update(names)
            rendered = ast.unparse(node).strip()
            if rendered:
                body_texts.append(rendered)

    chunks = [*import_texts, *body_texts]
    if not chunks:
        return "", []
    return "\n\n".join(chunks).rstrip() + "\n", []


async def run_build(
    *,
    package_dir: Path,
    generated_dir: str,
    module_specs: dict[str, list[SpecEntry]],
    specs: dict[SpecRef, SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
    module_dag: dict[str, set[str]],
    stale_modules: set[str],
    changed_modules: set[str] | None = None,
    backend: GeneratorBackend,
    generation_fingerprint: str = "",
    skills_block: str = "",
    jobs: int = 4,
    progress: object | None = None,
    response_cache: ResponseCache | None = None,
    cost_tracker: CostTracker | None = None,
    ty_retry_attempts: int | None = None,
    async_runner: str = "asyncio",
    initial_error_context_by_module: dict[str, list[str]] | None = None,
) -> BuildReport:
    jobs = max(1, int(jobs))
    ty_attempts = max(0, int(ty_retry_attempts)) if ty_retry_attempts is not None else None

    # Expand rebuild set and restrict to modules we actually have specs for.
    expanded = expand_stale_modules(
        module_dag,
        set(stale_modules),
        changed_modules=(set(changed_modules) if changed_modules is not None else None),
    )
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
    # Track generated source for dependency context injection.
    generated_sources: dict[str, str] = {}
    failed: dict[str, list[str]] = {}
    completed: set[str] = set()
    ty_cmd = _resolve_ty_cmd() if ty_attempts is not None else None
    llm_slots = asyncio.Semaphore(jobs)

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
                    dep_apis[dep_entry.spec_ref] = build_dependency_api_block(dep_entry)
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

    async def _generate_ctx(
        module_name: str,
        ctx: ModuleSpecContext,
        *,
        validate_candidate: Callable[[str], list[str]],
        retry_validator: Callable[[str], list[str]],
    ) -> tuple[bool, str | None, list[str]]:
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
                cache_errors = validate_candidate(cached.source)
                if not cache_errors:
                    result_source = cached.source
                    if cost_tracker is not None:
                        cost_tracker.record_cache_hit()

        if result_source is None:
            max_attempts = (2 + (ty_attempts or 0)) if ty_cmd is not None else 2
            async with llm_slots:
                result = await backend.generate_with_retry(
                    ctx,
                    max_attempts=max_attempts,
                    extra_validator=retry_validator,
                    initial_error_context=(initial_error_context_by_module or {}).get(module_name),
                )
            if result.source is None:
                return False, None, result.errors or ["No source returned."]
            if result.errors:
                return False, None, result.errors

            result_source = result.source
            validation_errors = validate_candidate(result_source)
            if validation_errors:
                return False, None, validation_errors

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

        return True, result_source, []

    async def build_one(module_name: str) -> tuple[bool, list[str]]:
        entries = module_specs.get(module_name, [])

        expected, conflict_errs = _build_expected_names(entries)
        if conflict_errs:
            return False, conflict_errs

        dep_apis, dep_gen = _collect_dependency_context(module_name)
        all_generated_names = list(expected)

        ty_validator: Callable[[str], list[str]] | None = None
        if ty_cmd is not None:
            ty_cmd_local = ty_cmd

            def _local_ty_validator(source: str) -> list[str]:
                return _ty_error_context(
                    source=source,
                    module_name=module_name,
                    package_dir=package_dir,
                    generated_dir=generated_dir,
                    ty_cmd=ty_cmd_local,
                )

            ty_validator = _local_ty_validator

        def _component_payload(
            component_entries: list[SpecEntry],
        ) -> tuple[ModuleSpecContext, tuple[str, ...], tuple[str, ...]]:
            component_expected, component_conflict_errs = _build_expected_names(component_entries)
            if component_conflict_errs:
                raise ValueError("\n".join(component_conflict_errs))

            spec_sources: dict[SpecRef, str] = {}
            decorator_prompts: dict[SpecRef, str] = {}
            decorator_apis: dict[SpecRef, str] = {}
            for entry in component_entries:
                spec_sources[entry.spec_ref] = extract_source_segment(entry)
                prompt = entry.decorator_kwargs.get("prompt")
                if isinstance(prompt, str) and prompt:
                    decorator_prompts[entry.spec_ref] = prompt
                lines: list[str] = []
                if entry.effective_signature is not None:
                    src = entry.effective_signature_source or "unknown"
                    lines.append(f"effective_signature[{src}]: {entry.effective_signature}")
                for rec in entry.decorator_api_records:
                    lines.append(
                        f"{rec.symbol_path} ({rec.position}) "
                        f"target={rec.resolved_target or '<unknown>'} "
                        f"signature={rec.signature or '<missing>'} "
                        f"quality={rec.annotation_quality}"
                    )
                for warning in entry.decorator_warnings:
                    lines.append(f"warning: {warning}")
                if lines:
                    decorator_apis[entry.spec_ref] = "\n".join(lines)

            component_contract = build_module_contract(
                entries=entries,
                expected_names=component_expected,
                generated_names=all_generated_names,
            )
            ctx = ModuleSpecContext(
                kind="build",
                spec_module=module_name,
                generated_module=paths.spec_module_to_generated_module(
                    module_name, generated_dir=generated_dir
                ),
                expected_names=component_expected,
                spec_sources=spec_sources,
                decorator_prompts=decorator_prompts,
                dependency_apis=dep_apis,
                dependency_generated_modules=dep_gen,
                decorator_apis=decorator_apis,
                skills_block=skills_block,
                module_contract_block=component_contract.prompt_block,
                module_context_digest=component_contract.digest,
                async_runner=async_runner,
            )
            return ctx, tuple(component_expected), component_contract.handwritten_names

        def _make_validators(
            *,
            component_expected: list[str],
            handwritten_names: tuple[str, ...],
        ) -> tuple[Callable[[str], list[str]], Callable[[str], list[str]]]:
            def _validate_candidate(source: str) -> list[str]:
                errs = validate_build_generated_source(
                    source,
                    component_expected,
                    spec_module=module_name,
                    handwritten_names=handwritten_names,
                )
                if errs:
                    return errs
                if ty_validator is None:
                    return []
                return ty_validator(source)

            def _retry_validator(source: str) -> list[str]:
                errs = validate_build_contract_only(
                    source,
                    expected_names=component_expected,
                    spec_module=module_name,
                    handwritten_names=handwritten_names,
                )
                if errs:
                    return errs
                if ty_validator is None:
                    return []
                return ty_validator(source)

            return _validate_candidate, _retry_validator

        module_contract = build_module_contract(
            entries=entries,
            expected_names=expected,
            generated_names=all_generated_names,
        )

        def _validate_module_candidate(source: str) -> list[str]:
            errs = validate_build_generated_source(
                source,
                expected,
                spec_module=module_name,
                handwritten_names=module_contract.handwritten_names,
            )
            if errs:
                return errs
            if ty_validator is None:
                return []
            return ty_validator(source)

        components = _component_entries(
            module_name=module_name,
            entries=entries,
            spec_graph=spec_graph,
        )
        result_source: str | None = None
        split_errors: list[str] = []

        if len(components) > 1 and jobs > 1:

            async def _build_component(
                component_entries: list[SpecEntry],
            ) -> tuple[bool, _GeneratedComponent | None, list[str]]:
                try:
                    ctx, component_expected, handwritten_names = _component_payload(
                        component_entries
                    )
                except ValueError as exc:
                    return False, None, [str(exc)]
                validate_candidate, retry_validator = _make_validators(
                    component_expected=list(component_expected),
                    handwritten_names=handwritten_names,
                )
                ok, source, errs = await _generate_ctx(
                    module_name,
                    ctx,
                    validate_candidate=validate_candidate,
                    retry_validator=retry_validator,
                )
                if not ok or source is None:
                    return False, None, errs
                return (
                    True,
                    _GeneratedComponent(expected_names=component_expected, source=source),
                    [],
                )

            component_results = await asyncio.gather(
                *[asyncio.create_task(_build_component(component)) for component in components]
            )
            generated_components: list[_GeneratedComponent] = []
            for ok, generated_component, errs in component_results:
                if not ok or generated_component is None:
                    split_errors.extend(errs)
                else:
                    generated_components.append(generated_component)

            if not split_errors:
                merged_source, merge_errors = _merge_generated_components(generated_components)
                if merge_errors:
                    split_errors.extend(merge_errors)
                else:
                    validation_errors = _validate_module_candidate(merged_source)
                    if validation_errors:
                        split_errors.extend(validation_errors)
                    else:
                        result_source = merged_source

        if result_source is None:
            ctx, _component_expected, handwritten_names = _component_payload(entries)
            validate_candidate, retry_validator = _make_validators(
                component_expected=expected,
                handwritten_names=handwritten_names,
            )
            ok, source, errs = await _generate_ctx(
                module_name,
                ctx,
                validate_candidate=validate_candidate,
                retry_validator=retry_validator,
            )
            if not ok or source is None:
                if split_errors:
                    return False, [*split_errors, *errs]
                return False, errs
            result_source = source

        generated_sources[module_name] = result_source

        digest = module_digest(module_name, entries, specs, spec_graph)
        header_fields = {
            "tool_version": _tool_version(),
            "kind": "build",
            "source_module": module_name,
            "module_digest": digest,
            "generation_fingerprint": generation_fingerprint,
            "module_context_digest": module_contract.digest,
            "module_api_digest": module_api_digest(entries),
            "spec_refs": [str(e.spec_ref) for e in entries],
        }

        write_generated_module(
            package_dir=package_dir,
            generated_dir=generated_dir,
            module_name=module_name,
            source=result_source,
            header_fields=header_fields,
        )
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
