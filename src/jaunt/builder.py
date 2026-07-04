"""The forge: build orchestration and parallel scheduling.

What the hammer? what the chain? -- specs enter the furnace, implementations
emerge on the other side.
"""

from __future__ import annotations

import asyncio
import ast
import hashlib
import heapq
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from jaunt import header, paths
from jaunt.agent_docs import ensure_agent_docs
from jaunt.cache import CacheEntry, ResponseCache, cache_key_from_context
from jaunt.change_detection import (
    assess_specs,
    classify_change,
    read_contract_sidecar,
    sidecar_path,
    write_contract_sidecar,
)
from jaunt.config import SemanticGateConfig
from jaunt.cost import CostTracker
from jaunt.decorator_analysis import _is_magic_decorator
from jaunt.digest import (
    contract_snapshot,
    extract_source_segment,
    module_digest,
    prose_digest,
    structural_digest,
)
from jaunt.errors import JauntDependencyCycleError, JauntError, JauntGenerationError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.generate.shared import fmt_kv_block
from jaunt.header import (
    extract_base_api_digest,
    extract_digest_scheme,
    extract_generation_fingerprint,
    extract_module_api_digest,
    extract_module_context_digest,
    extract_module_digest,
    format_header,
)
from jaunt.module_api import build_dependency_api_block, module_api_digest
from jaunt.module_contract import (
    build_module_contract,
)
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef
from jaunt.validation import (
    class_build_warnings,
    validate_build_class_source,
    validate_build_contract_only,
    validate_build_generated_source,
)

_TY_CHECK_TIMEOUT_S = 20.0
NEEDS_DEP_MARKER = "JAUNT-NEEDS-DEP:"


def _scan_needs_dep_markers(source: str) -> list[str]:
    markers: list[str] = []
    for line in source.splitlines():
        idx = line.find(NEEDS_DEP_MARKER)
        if idx != -1:
            markers.append(line[idx:].strip())
    return markers


def _block_size(text: str) -> dict[str, int]:
    chars = len(text or "")
    return {"chars": chars, "est_tokens": chars // 4}


def _skills_workspace_chars(project_root: Path | None) -> int:
    """Total chars of SKILL.md files seeded under <project_root>/.agents/skills/."""
    if project_root is None:
        return 0
    skills_root = project_root / ".agents" / "skills"
    if not skills_root.is_dir():
        return 0
    total = 0
    for path in skills_root.rglob("SKILL.md"):
        try:
            total += len(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return total


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


def _read_generated(package_dir: Path, generated_dir: str, module_name: str) -> str | None:
    relpath = _generated_relpath(module_name, generated_dir=generated_dir)
    try:
        return (package_dir / relpath).read_text(encoding="utf-8")
    except Exception:
        return None


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
    spec_digests: dict[str, dict[str, str]] | None = None,
    snapshots: dict[str, dict] | None = None,
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

    local_fields = dict(header_fields)
    if spec_digests is not None:
        local_fields["spec_digests"] = spec_digests
        local_fields["digest_scheme"] = 2
    hdr = format_header(**local_fields)
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
        if snapshots is not None:
            write_contract_sidecar(sidecar_path(out_path), snapshots)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return out_path


def _compute_spec_digests(entries: list[SpecEntry]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for entry in entries:
        out[str(entry.spec_ref)] = {
            "s": structural_digest(entry),
            "p": prose_digest(entry),
        }
    return out


def _compute_snapshots(entries: list[SpecEntry]) -> dict[str, dict]:
    return {str(entry.spec_ref): contract_snapshot(entry) for entry in entries}


def detect_stale_modules(
    *,
    package_dir: Path,
    generated_dir: str,
    module_specs: dict[str, list[SpecEntry]],
    specs: dict[SpecRef, SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
    generation_fingerprint: str = "",
    module_context_digests: dict[str, str] | None = None,
    module_base_api_digests: dict[str, str] | None = None,
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
        if module_name in stale:
            continue
        if module_base_api_digests is not None:
            computed_base = module_base_api_digests.get(module_name)
            if computed_base:
                on_disk_base = _normalize_digest(extract_base_api_digest(existing))
                norm_computed_base = _normalize_digest(computed_base)
                if on_disk_base is None or on_disk_base != norm_computed_base:
                    stale.add(module_name)
                    continue

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
    allowed_modules: set[str] | None = None,
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
            if allowed_modules is not None and dep not in allowed_modules:
                continue
            if dep in expanded:
                continue
            expanded.add(dep)
            queue.append(dep)
    return expanded


@dataclass(frozen=True, slots=True)
class RefreezeOutcome:
    refrozen: bool
    needs_rebuild: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RefreezePlan:
    rebuild: set[str]
    refrozen: set[str]
    failed_refreeze: set[str]


def _strip_header(existing: str) -> str | None:
    lines = existing.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != header.HEADER_MARKER:
        return None
    try:
        header.parse_header(existing)
    except Exception:
        return None

    i = 1
    while i < len(lines) and lines[i].startswith("# jaunt:"):
        i += 1
    if i < len(lines) and lines[i].strip() == "":
        i += 1
    return "".join(lines[i:])


def refreeze_module(
    *,
    package_dir: Path,
    generated_dir: str,
    module_name: str,
    header_fields: dict[str, object],
    snapshots: dict[str, dict],
    validate_body: Callable[[str], list[str]] | None = None,
) -> RefreezeOutcome:
    relpath = _generated_relpath(module_name, generated_dir=generated_dir)
    module_path = package_dir / relpath
    try:
        existing = module_path.read_text(encoding="utf-8")
    except Exception as exc:
        return RefreezeOutcome(
            refrozen=False,
            needs_rebuild=True,
            errors=(f"Unable to read generated module {module_name}: {exc}",),
        )

    body = _strip_header(existing)
    if body is None:
        return RefreezeOutcome(
            refrozen=False,
            needs_rebuild=True,
            errors=(f"Generated module {module_name} has no Jaunt header.",),
        )

    if validate_body is None:
        from jaunt.validation import compile_check

        errors = compile_check(body, module_name)
    else:
        errors = validate_body(body)
    if errors:
        return RefreezeOutcome(refrozen=False, needs_rebuild=True, errors=tuple(errors))

    spec_digests = cast(dict[str, dict[str, str]] | None, header_fields.get("spec_digests"))
    local_fields = dict(header_fields)
    local_fields.pop("spec_digests", None)
    local_fields.pop("legacy_module_digest", None)
    write_generated_module(
        package_dir=package_dir,
        generated_dir=generated_dir,
        module_name=module_name,
        source=body,
        header_fields=local_fields,
        spec_digests=spec_digests,
        snapshots=snapshots,
    )
    return RefreezeOutcome(refrozen=True, needs_rebuild=False)


def _header_fields_with_spec_digests(
    header_fields: dict[str, object],
    entries: list[SpecEntry],
) -> dict[str, object]:
    local_fields = dict(header_fields)
    if "spec_digests" not in local_fields:
        local_fields["spec_digests"] = _compute_spec_digests(entries)
    return local_fields


def _header_field_matches(
    existing: str,
    header_fields: dict[str, object],
    key: str,
    extractor: Callable[[str], str | None],
) -> bool:
    on_disk = _normalize_digest(extractor(existing))
    raw_computed = header_fields.get(key)
    computed = _normalize_digest(str(raw_computed)) if raw_computed is not None else None
    if computed is None:
        return on_disk is None
    return on_disk == computed


def _migration_header_matches(existing: str, header_fields: dict[str, object]) -> bool:
    # Compare the on-disk (scheme-1, raw-source) module digest against the
    # *recomputed legacy* digest, not the new normalized one -- otherwise a real
    # pre-upgrade file never matches and every module gets rebuilt on first build.
    if not _header_field_matches(
        existing, header_fields, "legacy_module_digest", extract_module_digest
    ):
        return False
    return all(
        (
            _header_field_matches(
                existing,
                header_fields,
                "generation_fingerprint",
                extract_generation_fingerprint,
            ),
            _header_field_matches(
                existing,
                header_fields,
                "module_context_digest",
                extract_module_context_digest,
            ),
            _header_field_matches(
                existing,
                header_fields,
                "module_api_digest",
                extract_module_api_digest,
            ),
        )
    )


async def plan_refreeze_or_rebuild(
    *,
    package_dir: Path,
    generated_dir: str,
    module_specs: dict[str, list[SpecEntry]],
    specs: dict[SpecRef, SpecEntry],
    spec_graph: dict[SpecRef, set[SpecRef]],
    module_dag: dict[str, set[str]],
    stale_modules: set[str],
    header_fields_by_module: dict[str, dict[str, object]],
    cfg: SemanticGateConfig,
    base_api_changed: set[str] | frozenset[str] = frozenset(),
    gate_enabled: bool = True,
    validators_by_module: dict[str, Callable[[str], list[str]]] | None = None,
    run_exec=None,
) -> RefreezePlan:
    del specs, spec_graph

    rebuild: set[str] = set()
    refrozen: set[str] = set()
    failed_refreeze: set[str] = set()
    gate_modules: set[str] = set()
    validators = validators_by_module or {}

    for module_name in sorted(stale_modules):
        if module_name in base_api_changed:
            # A spec'd base's generated public API moved (or was never captured):
            # the generated body may genuinely need to change -- never refreeze.
            rebuild.add(module_name)
            continue
        entries = module_specs.get(module_name, [])
        header_fields = _header_fields_with_spec_digests(
            header_fields_by_module.get(module_name, {}),
            entries,
        )
        snapshots = _compute_snapshots(entries)
        existing = _read_generated(package_dir, generated_dir, module_name)
        if existing is None:
            rebuild.add(module_name)
            continue

        scheme = extract_digest_scheme(existing)
        if scheme is None or scheme < 2:
            if _migration_header_matches(existing, header_fields):
                outcome = refreeze_module(
                    package_dir=package_dir,
                    generated_dir=generated_dir,
                    module_name=module_name,
                    header_fields=header_fields,
                    snapshots=snapshots,
                    validate_body=validators.get(module_name),
                )
                if outcome.needs_rebuild:
                    failed_refreeze.add(module_name)
                    rebuild.add(module_name)
                elif outcome.refrozen:
                    refrozen.add(module_name)
                continue
        gate_modules.add(module_name)

    meaningful_modules: set[str] = set()
    for module_name in sorted(gate_modules):
        entries = module_specs.get(module_name, [])
        relpath = _generated_relpath(module_name, generated_dir=generated_dir)
        module_file = package_dir / relpath
        old_snapshots = read_contract_sidecar(sidecar_path(module_file))
        if gate_enabled:
            if run_exec is not None:
                verdicts = await assess_specs(entries, old_snapshots, cfg, run_exec=run_exec)
            else:
                verdicts = await assess_specs(entries, old_snapshots, cfg)
            if any(verdicts.get(entry.spec_ref) == "MEANINGFUL" for entry in entries):
                meaningful_modules.add(module_name)
        elif any(
            classify_change(old_snapshots.get(str(entry.spec_ref)), entry) != "none"
            for entry in entries
        ):
            meaningful_modules.add(module_name)

    rebuild |= expand_stale_modules(
        module_dag,
        set(meaningful_modules),
        changed_modules=set(meaningful_modules),
        allowed_modules=set(stale_modules),
    )
    rebuild &= set(stale_modules)

    for module_name in sorted(set(stale_modules) - rebuild - failed_refreeze - refrozen):
        entries = module_specs.get(module_name, [])
        header_fields = _header_fields_with_spec_digests(
            header_fields_by_module.get(module_name, {}),
            entries,
        )
        outcome = refreeze_module(
            package_dir=package_dir,
            generated_dir=generated_dir,
            module_name=module_name,
            header_fields=header_fields,
            snapshots=_compute_snapshots(entries),
            validate_body=validators.get(module_name),
        )
        if outcome.needs_rebuild:
            failed_refreeze.add(module_name)
            rebuild.add(module_name)
        elif outcome.refrozen:
            refrozen.add(module_name)

    rebuild &= set(stale_modules)
    refrozen -= rebuild
    return RefreezePlan(rebuild=rebuild, refrozen=refrozen, failed_refreeze=failed_refreeze)


@dataclass(frozen=True, slots=True)
class BuildReport:
    generated: set[str]
    skipped: set[str]
    failed: dict[str, list[str]]
    needs_deps: dict[str, list[str]] = field(default_factory=dict)
    context_stats: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    emitted_stubs: dict[str, str] = field(default_factory=dict)
    stub_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _GeneratedComponent:
    expected_names: tuple[str, ...]
    source: str


@dataclass(frozen=True, slots=True)
class BuildModuleContextArtifacts:
    module_contract_block: str
    base_contract_block: str
    blueprint_source: str
    build_instructions_block: str
    attached_test_specs_block: str
    package_context_block: str
    handwritten_names: tuple[str, ...]
    digest: str


def _module_context_stats(
    *,
    artifacts: BuildModuleContextArtifacts,
    whole_class_contract_block: str,
    dep_apis: dict[SpecRef, str],
    dep_gen: dict[str, str],
    repo_map_block: str,
    project_overview_block: str,
    preamble_chars: int,
    skills_workspace_chars: int,
) -> dict[str, dict[str, int]]:
    """Per-block char / estimated-token accounting for one built module.

    est_tokens is a coarse chars // 4 estimate. Blocks mirror what the build prompt
    assembles: the static preamble, system/build directives, the module contract,
    dependency APIs + generated dep sources, package grounding, the repo map (plus any
    project overview), the blueprint source, and the seeded skills workspace.
    """
    deps_text = "".join(dep_apis.values()) + "".join(dep_gen.values())
    module_contract_text = "".join(
        [
            artifacts.module_contract_block,
            artifacts.base_contract_block,
            whole_class_contract_block,
        ]
    )
    system_text = artifacts.build_instructions_block + artifacts.attached_test_specs_block
    repo_map_text = repo_map_block + project_overview_block
    return {
        "preamble": {"chars": preamble_chars, "est_tokens": preamble_chars // 4},
        "system": _block_size(system_text),
        "module_contract": _block_size(module_contract_text),
        "deps": _block_size(deps_text),
        "package_context": _block_size(artifacts.package_context_block),
        "repo_map": _block_size(repo_map_text),
        "blueprint": _block_size(artifacts.blueprint_source),
        "skills_workspace": {
            "chars": skills_workspace_chars,
            "est_tokens": skills_workspace_chars // 4,
        },
    }


@dataclass(frozen=True, slots=True)
class WholeClassContext:
    base_contract_block: str
    inherited_api_block: str
    whole_class_contract_block: str
    base_api_digest: str


def _whole_class_specs(entries: list[SpecEntry]) -> dict[str, SpecEntry]:
    """Map class name -> SpecEntry for whole-class @magic specs (obj is a type, no dot)."""
    out: dict[str, SpecEntry] = {}
    for e in entries:
        if e.class_name is None and "." not in e.qualname and isinstance(e.obj, type):
            out[e.qualname] = e
    return out


def _class_validation_inputs(entry: SpecEntry) -> dict[str, object]:
    import ast as _ast

    from jaunt.class_analysis import (
        canonical_signature,
        classify_class_mode,
        is_preserve_decorator,
        resolve_base_contract,
    )
    from jaunt.class_analysis import split_class_members
    from jaunt.digest import extract_source_segment

    seg = extract_source_segment(entry)
    cls_node = _ast.parse(seg).body[0]
    assert isinstance(cls_node, _ast.ClassDef)
    split = split_class_members(cls_node)
    methods = {
        n.name: n for n in cls_node.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
    }
    class_attributes: dict[str, str] = {}
    for node in cls_node.body:
        if isinstance(node, _ast.Assign):
            rendered = _ast.unparse(node)
            for t in node.targets:
                if isinstance(t, _ast.Name):
                    class_attributes[t.id] = rendered
        elif isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
            class_attributes[node.target.id] = _ast.unparse(node)
    preserved_segments: dict[str, str] = {}
    for name in split.preserved:
        node = methods[name]
        # Strip @jaunt.preserve before storing for comparison.
        clone = _ast.parse(_ast.unparse(node)).body[0]
        assert isinstance(clone, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        clone.decorator_list = [d for d in clone.decorator_list if not is_preserve_decorator(d)]
        preserved_segments[name] = _ast.unparse(clone)
    contract = resolve_base_contract(entry.obj)  # type: ignore[arg-type]
    return {
        "class_name": entry.qualname,
        "stub_methods": list(split.stubs),
        "preserved_segments": preserved_segments,
        "declared_bases": [_ast.unparse(b) for b in cls_node.bases],
        "class_decorators": [
            _ast.unparse(d) for d in cls_node.decorator_list if not _is_magic_decorator(d)
        ],
        "required_abstractmethods": list(contract.required_abstractmethods),
        "spec_docstring": _ast.get_docstring(cls_node, clean=True) or "",
        "class_attributes": class_attributes,
        "require_public_method": classify_class_mode(cls_node) == "docstring_only",
        "sealed_signatures": {name: canonical_signature(methods[name]) for name in split.sealed},
    }


def _class_warning_inputs(entry: SpecEntry) -> dict[str, object]:
    import ast as _ast

    from jaunt.class_analysis import split_class_members
    from jaunt.digest import extract_source_segment

    seg = extract_source_segment(entry)
    cls_node = _ast.parse(seg).body[0]
    assert isinstance(cls_node, _ast.ClassDef)
    split = split_class_members(cls_node)
    methods = {
        n.name: n for n in cls_node.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
    }
    stub_signatures: dict[str, list[str]] = {}
    for name in split.stubs:
        node = methods[name]
        stub_signatures[name] = [
            *(arg.arg for arg in node.args.posonlyargs),
            *(arg.arg for arg in node.args.args),
            *(arg.arg for arg in node.args.kwonlyargs),
        ]
    return {
        "class_name": entry.qualname,
        "stub_signatures": stub_signatures,
    }


def _render_inherited_signature(signature: str, name: str) -> str:
    """Render a generated member's signature for the inherited-API block.

    Strips the leading ``def ``/``async def ``/``class `` keyword so the line reads as
    ``ClassName.method(args) -> ret`` when prefixed with the owning class name.
    """

    for prefix in ("async def ", "def ", "class "):
        if signature.startswith(prefix):
            return signature[len(prefix) :]
    return signature or name


def _whole_class_context(
    entries: list[SpecEntry],
    *,
    specs: dict[SpecRef, SpecEntry],
    package_dir: Path,
    generated_dir: str,
) -> WholeClassContext:
    """Assemble base-class context for the whole-class specs in ``entries``."""

    from jaunt.class_analysis import render_whole_class_contract, resolve_base_contract
    from jaunt.digest import extract_source_segment
    from jaunt.module_api import build_generated_class_api_summary

    whole = _whole_class_specs(entries)
    base_blocks: list[str] = []
    inherited_lines: list[str] = []
    contract_blocks: list[str] = []

    for entry in whole.values():
        # Cross-module spec'd bases are represented below by the artifact-derived
        # inherited-API block, not the runtime MRO snapshot. Excluding them from the
        # runtime base-contract block keeps the hashed context stable across a build
        # and a later `jaunt status` re-import: the runtime base object reflects
        # import-time build state (a placeholder before the base is built, the
        # generated class after), which would otherwise flap the digest. External /
        # non-spec bases and same-module co-generated bases keep runtime rendering.
        cross_module_base_refs: set[str] = set()
        for dep_ref in entry.base_deps:
            base_spec = specs.get(dep_ref)
            if base_spec is not None and base_spec.module != entry.module:
                cross_module_base_refs.add(str(dep_ref))
        base_block = resolve_base_contract(
            entry.obj,  # type: ignore[arg-type]
            exclude_refs=cross_module_base_refs,
        ).block
        base_blocks.append(base_block)

        entry_inherited: list[str] = []
        for dep_ref in entry.base_deps:
            dep = specs.get(dep_ref)
            if dep is None or dep.module == entry.module:
                continue
            gen_path = package_dir / _generated_relpath(dep.module, generated_dir=generated_dir)
            try:
                gen_src = gen_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # Base genuinely not built yet -> fixed sentinel (design §5).
                entry_inherited.append(f"unbuilt:{dep_ref!s}")
                continue
            try:
                summary = build_generated_class_api_summary(
                    gen_src,
                    dep.qualname,
                    spec_docstring="",
                )
            except Exception as exc:
                # A present-but-corrupt base artifact (syntax error, missing class)
                # must surface, not silently degrade to the `unbuilt` sentinel.
                raise JauntError(
                    f"Base generated artifact for {dep_ref!s} at {gen_path} could not be "
                    f"summarized ({exc}); the base's generated code is missing its class "
                    "or is not valid Python."
                ) from exc
            for member in summary.members:
                rendered_signature = _render_inherited_signature(member.signature, member.name)
                entry_inherited.append(f"{dep.qualname}.{rendered_signature}")
                if member.doc:
                    # Hash the full docstring, not just the first line: docstrings are
                    # behavioral contract, so a second-line-only change to a base's
                    # public API must restale the subclass (design §5).
                    doc_lines = member.doc.splitlines()
                    if len(doc_lines) == 1:
                        entry_inherited.append(f"  doc: {doc_lines[0]}")
                    else:
                        entry_inherited.append("  doc:")
                        entry_inherited.extend(f"    {line}" for line in doc_lines)

        inherited_lines.extend(entry_inherited)
        contract_blocks.append(
            render_whole_class_contract(
                class_segment=extract_source_segment(entry),
                base_contract_block=base_block,
                inherited_api_block="\n".join(entry_inherited),
            )
        )

    inherited_api_block = "\n".join(inherited_lines)
    return WholeClassContext(
        base_contract_block="\n\n".join(base_blocks),
        inherited_api_block=inherited_api_block,
        whole_class_contract_block="\n\n".join(contract_blocks),
        base_api_digest=(
            hashlib.sha256(inherited_api_block.encode("utf-8")).hexdigest()
            if inherited_api_block
            else ""
        ),
    )


def build_module_context_artifacts(
    *,
    module_name: str,
    entries: list[SpecEntry],
    expected_names: list[str],
    generated_names: list[str] | None = None,
    module_specs: dict[str, list[SpecEntry]],
    module_dag: dict[str, set[str]],
    package_dir: Path,
    generated_dir: str,
    build_instructions: Sequence[str] | None = None,
    targeted_test_entries: dict[str, list[SpecEntry]] | None = None,
    base_contract_block: str = "",
    whole_class_contract_block: str = "",
    inherited_api_block: str = "",
) -> BuildModuleContextArtifacts:
    module_contract = build_module_contract(
        entries=entries,
        expected_names=expected_names,
        generated_names=generated_names,
    )
    blueprint_source = _build_blueprint_source(
        entries=entries,
        generated_names=generated_names or expected_names,
    )
    build_instructions_block = _build_instructions_block(build_instructions or [])
    attached_test_specs_block = _build_attached_test_specs_block(
        targeted_test_entries.get(module_name, []) if targeted_test_entries else []
    )
    package_context_block = _build_package_context_block(
        module_name=module_name,
        entries=entries,
        module_specs=module_specs,
        module_dag=module_dag,
        package_dir=package_dir,
        generated_dir=generated_dir,
    )
    digest = _build_context_digest(
        module_contract_block=module_contract.prompt_block,
        blueprint_source=blueprint_source,
        build_instructions_block=build_instructions_block,
        attached_test_specs_block=attached_test_specs_block,
        base_contract_block=base_contract_block,
        whole_class_contract_block=whole_class_contract_block,
        inherited_api_block=inherited_api_block,
    )
    return BuildModuleContextArtifacts(
        module_contract_block=module_contract.prompt_block,
        base_contract_block=base_contract_block,
        blueprint_source=blueprint_source,
        build_instructions_block=build_instructions_block,
        attached_test_specs_block=attached_test_specs_block,
        package_context_block=package_context_block,
        handwritten_names=module_contract.handwritten_names,
        digest=digest,
    )


def _build_context_digest(
    *,
    module_contract_block: str,
    blueprint_source: str,
    build_instructions_block: str,
    attached_test_specs_block: str,
    base_contract_block: str,
    whole_class_contract_block: str = "",
    inherited_api_block: str = "",
) -> str:
    h = hashlib.sha256()
    for block in (
        module_contract_block,
        base_contract_block,
        blueprint_source,
        build_instructions_block,
        attached_test_specs_block,
    ):
        h.update((block or "").encode("utf-8"))
        h.update(b"\x00")
    for block in (whole_class_contract_block, inherited_api_block):
        if block:
            h.update(block.encode("utf-8"))
            h.update(b"\x00")
    return h.hexdigest()


def _build_instructions_block(instructions: Sequence[str]) -> str:
    lines = [value.strip() for value in instructions if value.strip()]
    if not lines:
        return ""
    return "\n".join(f"- {line}" for line in lines) + "\n"


def _build_blueprint_source(*, entries: list[SpecEntry], generated_names: list[str]) -> str:
    if not entries:
        return ""

    source_file = entries[0].source_file
    spec_module = entries[0].module
    source = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=source_file)
    generated = set(generated_names)
    chunks: list[str] = []
    handwritten_names = _blueprint_handwritten_names(tree, generated=generated)
    inserted_reference_header = False

    for index, node in enumerate(tree.body):
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(getattr(node, "value", None), ast.Constant)
            and isinstance(getattr(node.value, "value", None), str)
        ):
            rendered = _clean_source_segment(source, node)
            if rendered:
                chunks.append(rendered)
            continue

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            rendered = _clean_source_segment(source, node)
            if rendered:
                chunks.append(rendered)
            continue

        if not inserted_reference_header and handwritten_names:
            chunks.append(
                _render_blueprint_reference_header(
                    spec_module=spec_module,
                    handwritten_names=handwritten_names,
                )
            )
            inserted_reference_header = True

        names = _defined_top_level_names(node)
        if generated & names:
            rendered = _render_blueprint_stub(node)
            if rendered:
                chunks.append(rendered)
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            rendered = _render_blueprint_reference_marker(node, spec_module=spec_module)
            if rendered:
                chunks.append(rendered)
            continue

        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            rendered = _render_blueprint_reference_marker(node, spec_module=spec_module)
            if rendered:
                chunks.append(rendered)

    if not chunks:
        return ""
    return "\n\n".join(chunk.rstrip() for chunk in chunks if chunk.strip()).rstrip() + "\n"


def _render_blueprint_stub(node: ast.AST) -> str:
    prepared = ast.fix_missing_locations(ast.copy_location(node, node))
    transformed = _BlueprintTransformer().visit(prepared)
    if transformed is None:
        return ""
    module = ast.Module(body=[transformed], type_ignores=[])
    ast.fix_missing_locations(module)
    return ast.unparse(module).strip()


def _render_blueprint_reference_header(
    *,
    spec_module: str,
    handwritten_names: list[str],
) -> str:
    lines = [
        f"# Reference-only blueprint for `{spec_module}`.",
        "# `context/contract.md` is the authoritative source for handwritten definitions.",
        (
            f"# Reuse handwritten symbols from `{spec_module}`; "
            "do not copy them into generated output."
        ),
        "# Suggested import/reuse pattern:",
    ]
    if len(handwritten_names) == 1:
        lines.append(f"# from {spec_module} import {handwritten_names[0]}")
        return "\n".join(lines)

    lines.append(f"# from {spec_module} import (")
    lines.extend(f"#     {name}," for name in handwritten_names)
    lines.append("# )")
    return "\n".join(lines)


def _render_blueprint_reference_marker(node: ast.AST, *, spec_module: str) -> str:
    names = _top_level_names_in_order(cast(ast.stmt, node))
    if not names:
        return ""
    kind = _blueprint_node_kind(cast(ast.stmt, node))
    joined_names = ", ".join(names)
    return "\n".join(
        [
            f"# handwritten {kind} already defined in `{spec_module}`: {joined_names}",
            "# reuse the existing definition from the source module; do not copy it here.",
        ]
    )


def _blueprint_handwritten_names(tree: ast.Module, *, generated: set[str]) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if not isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Assign,
                ast.AnnAssign,
                ast.AugAssign,
            ),
        ):
            continue
        node_names = _top_level_names_in_order(node)
        if not node_names or generated & set(node_names):
            continue
        names.extend(node_names)
    return names


def _blueprint_node_kind(node: ast.stmt) -> str:
    if isinstance(node, ast.FunctionDef):
        return "function"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async function"
    if isinstance(node, ast.ClassDef):
        return "class"
    return "assignment"


class _BlueprintTransformer(ast.NodeTransformer):
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node = cast(ast.FunctionDef, self.generic_visit(node))
        node.decorator_list = [dec for dec in node.decorator_list if not _is_jaunt_decorator(dec)]
        node.body = [ast.Expr(value=ast.Constant(value=Ellipsis))]
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        node = cast(ast.AsyncFunctionDef, self.generic_visit(node))
        node.decorator_list = [dec for dec in node.decorator_list if not _is_jaunt_decorator(dec)]
        node.body = [ast.Expr(value=ast.Constant(value=Ellipsis))]
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        node = cast(ast.ClassDef, self.generic_visit(node))
        node.decorator_list = [dec for dec in node.decorator_list if not _is_jaunt_decorator(dec)]
        cleaned_body: list[ast.stmt] = []
        for child in node.body:
            if (
                isinstance(child, ast.Expr)
                and isinstance(getattr(child, "value", None), ast.Constant)
                and isinstance(getattr(child.value, "value", None), str)
            ):
                continue
            cleaned_body.append(child)
        node.body = cleaned_body or [ast.Pass()]
        return node


def _build_attached_test_specs_block(entries: list[SpecEntry]) -> str:
    if not entries:
        return ""

    rendered: list[tuple[str, str]] = []
    for entry in sorted(entries, key=lambda item: (item.module, item.qualname, str(item.spec_ref))):
        rendered.append((str(entry.spec_ref), extract_source_segment(entry)))
    return fmt_kv_block(rendered)


def _build_package_context_block(
    *,
    module_name: str,
    entries: list[SpecEntry],
    module_specs: dict[str, list[SpecEntry]],
    module_dag: dict[str, set[str]],
    package_dir: Path,
    generated_dir: str,
) -> str:
    if not entries:
        return ""

    package_root = Path(entries[0].source_file).resolve().parent
    tree_lines: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        if generated_dir in path.parts or "__pycache__" in path.parts:
            continue
        try:
            rel = path.resolve().relative_to(package_dir.resolve())
        except ValueError:
            rel = path.name
        tree_lines.append(str(rel).replace("\\", "/"))

    dep_lines = [dep for dep in sorted(module_dag.get(module_name, set())) if dep]

    module_package, _, _module_leaf = module_name.rpartition(".")
    sibling_items: list[tuple[str, str]] = []
    for sibling_name, sibling_entries in sorted(module_specs.items()):
        if sibling_name == module_name or sibling_name.rpartition(".")[0] != module_package:
            continue
        sibling_expected, sibling_errors = _build_expected_names(sibling_entries)
        if sibling_errors:
            continue
        sibling_contract = build_module_contract(
            entries=sibling_entries,
            expected_names=sibling_expected,
        )
        sibling_source = Path(sibling_entries[0].source_file).read_text(encoding="utf-8")
        sibling_tree = ast.parse(sibling_source, filename=sibling_entries[0].source_file)
        summary_lines = [f"summary: {_first_module_doc_line(sibling_tree) or '(none)'}"]
        generated = ", ".join(sibling_expected) if sibling_expected else "(none)"
        summary_lines.append(f"generated: {generated}")
        handwritten = (
            ", ".join(sibling_contract.handwritten_names)
            if sibling_contract.handwritten_names
            else "(none)"
        )
        summary_lines.append(f"handwritten: {handwritten}")
        sibling_items.append((sibling_name, "\n".join(summary_lines)))

    sections: list[str] = []
    if tree_lines:
        sections.append("## Package tree\n" + "\n".join(tree_lines))
    if dep_lines:
        sections.append("## Direct dependency modules\n" + "\n".join(dep_lines))
    if sibling_items:
        sections.append("## Sibling module summaries\n" + fmt_kv_block(sibling_items))
    block = "\n\n".join(section.rstrip() for section in sections if section.strip()).rstrip()
    return block + ("\n" if block else "")


def _clean_source_segment(source: str, node: ast.AST) -> str:
    seg = ast.get_source_segment(source, node) or ""
    if not seg:
        return ""
    lines = [line.rstrip() for line in seg.splitlines()]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _is_jaunt_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id == "jaunt"
            and target.attr in {"magic", "test"}
        )
    if isinstance(target, ast.Name):
        return target.id in {"magic", "test"}
    return False


def _first_module_doc_line(node: ast.Module) -> str:
    doc = ast.get_docstring(node, clean=True)
    if not doc:
        return ""
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


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


def _mirror_package_sources(package_dir: Path, tmp_root: Path, relpath: Path) -> None:
    """Copy the candidate's package subtree (``.py`` only) into the ty sandbox.

    The candidate file itself is skipped — its content comes from the in-flight
    candidate source, not disk.
    """
    parts = relpath.parts
    if not parts:
        return
    subtree = package_dir / parts[0]
    if not subtree.is_dir():
        return
    for src in subtree.rglob("*.py"):
        if "__pycache__" in src.parts:
            continue
        rel = src.relative_to(package_dir)
        if rel == relpath:
            continue
        dest = tmp_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(src, dest)
        except OSError:
            continue


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
        # Mirror the candidate's package subtree into the sandbox. The sandbox
        # root shadows package_dir in ty's search path, so without the mirror a
        # bare `<pkg>/` containing only the candidate hides the real source
        # modules — legitimate imports like `from ..specs import X` become
        # unresolved and their targets type as Unknown.
        _mirror_package_sources(package_dir, tmp_root, relpath)
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
        # Order diagnostic blocks so unresolved-import noise cannot bury the
        # errors that actually fail the build (this text is also the model's
        # retry feedback).
        blocks: list[list[str]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            if re.match(r"(error|warning)\[", line) or not blocks:
                blocks.append([])
            blocks[-1].append(line)
        ordered = [b for b in blocks if not b[0].startswith("error[unresolved-import]")] + [
            b for b in blocks if b[0].startswith("error[unresolved-import]")
        ]
        lines = [line for block in ordered for line in block]
        snippet = "\n".join(lines[:40])
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
            f"Conflicting @magic: class(es) {names} have both whole-class @magic and "
            "per-method @magic registry entries. Inner @magic methods of a whole-class "
            "spec should have been absorbed at import time; this indicates a "
            "registration bug (or a hand-constructed registry)."
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
    return set(_top_level_names_in_order(node))


def _top_level_names_in_order(node: ast.stmt) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


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
    allowed_modules: set[str] | None = None,
    backend: GeneratorBackend,
    generation_fingerprint: str = "",
    repo_map_block: str = "",
    project_overview_block: str = "",
    search_enabled: bool = False,
    search_max_hits: int = 8,
    project_root: Path | None = None,
    source_roots: Sequence[Path] | None = None,
    builtin_skill_names: Sequence[str] = (),
    skills_digest: str = "",
    jobs: int = 4,
    progress: object | None = None,
    response_cache: ResponseCache | None = None,
    cost_tracker: CostTracker | None = None,
    ty_retry_attempts: int | None = None,
    async_runner: str = "asyncio",
    build_instructions: Sequence[str] | None = None,
    check_generated_imports: bool = True,
    generated_import_allowlist: Sequence[str] | None = None,
    initial_error_context_by_module: dict[str, list[str]] | None = None,
    targeted_test_entries: dict[str, list[SpecEntry]] | None = None,
    emit_stubs: bool = False,
) -> BuildReport:
    jobs = max(1, int(jobs))
    ty_attempts = max(0, int(ty_retry_attempts)) if ty_retry_attempts is not None else None

    # Expand rebuild set and restrict to modules we actually have specs for.
    expanded = expand_stale_modules(
        module_dag,
        set(stale_modules),
        changed_modules=(set(changed_modules) if changed_modules is not None else None),
        allowed_modules=allowed_modules,
    )
    stale = expanded & set(module_specs.keys())
    skipped = set(module_specs.keys()) - stale

    first_party_modules = {
        generated_dir,
        *(module_name.split(".", 1)[0] for module_name in module_specs),
    }
    generated_import_allowlist = tuple(generated_import_allowlist or ())
    validation_source_roots = tuple(
        (root if root.is_absolute() else package_dir / root).resolve()
        for root in (source_roots or (package_dir,))
    )

    def _validate_skipped_generated_modules(modules: set[str]) -> dict[str, list[str]]:
        if not check_generated_imports:
            return {}
        failures: dict[str, list[str]] = {}
        for module_name in sorted(modules):
            relpath = _generated_relpath(module_name, generated_dir=generated_dir)
            gen_path = package_dir / relpath
            try:
                source = gen_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError as exc:
                failures[module_name] = [f"Failed reading generated module: {exc}"]
                continue
            errs = validate_build_generated_source(
                source,
                [],
                spec_module=module_name,
                handwritten_names=(),
                generated_module=paths.spec_module_to_generated_module(
                    module_name, generated_dir=generated_dir
                ),
                project_dir=package_dir,
                source_roots=validation_source_roots,
                first_party_modules=first_party_modules,
                check_imports=True,
                import_allowlist=generated_import_allowlist,
            )
            if errs:
                failures[module_name] = errs
        return failures

    # For a targeted build (`allowed_modules` set, e.g. `jaunt build --target`),
    # `skipped` spans the whole project; only validate skipped modules within the
    # requested closure so an unrelated, out-of-target module never fails a
    # targeted build. A full build (`allowed_modules is None`) validates all skipped.
    skipped_to_validate = skipped if allowed_modules is None else skipped & set(allowed_modules)
    skipped_failures = _validate_skipped_generated_modules(skipped_to_validate)
    skipped -= set(skipped_failures)

    def _emit_stubs(
        generated_modules: set[str],
        skipped_modules: set[str],
    ) -> tuple[dict[str, str], list[str]]:
        if not emit_stubs:
            return {}, []
        from jaunt import stub_emitter

        emitted_stubs: dict[str, str] = {}
        stub_warnings: list[str] = []
        for module_name in sorted((generated_modules | skipped_modules) & set(module_specs.keys())):
            entries = module_specs.get(module_name, [])
            if not entries:
                continue
            source_file = entries[0].source_file
            gen_source = _read_generated(package_dir, generated_dir, module_name)
            if gen_source is None:
                continue
            expected, _ = _build_expected_names(entries)
            try:
                spec_source = Path(source_file).read_text(encoding="utf-8")
                stub_path = stub_emitter.stub_path_for_source(source_file)
                if stub_path.exists() and not stub_emitter.is_jaunt_stub(stub_path):
                    stub_warnings.append(
                        f"{module_name}: existing hand-authored {stub_path.name} not overwritten"
                    )
                    continue
                stub_header = header.format_stub_header(
                    tool_version=_tool_version(),
                    source_module=module_name,
                    generated_digest=stub_emitter.generated_content_digest(gen_source),
                )
                new_stub = stub_emitter.build_stub_source(
                    spec_source,
                    gen_source,
                    set(expected),
                    stub_header,
                )
                if not (stub_path.exists() and stub_path.read_text(encoding="utf-8") == new_stub):
                    stub_path.parent.mkdir(parents=True, exist_ok=True)
                    fd, tmp = tempfile.mkstemp(
                        dir=str(stub_path.parent),
                        prefix=".jaunt-stub-tmp-",
                        suffix=".pyi",
                        text=True,
                    )
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                            f.write(new_stub)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(tmp, stub_path)
                    finally:
                        try:
                            os.unlink(tmp)
                        except FileNotFoundError:
                            pass
                emitted_stubs[module_name] = str(stub_path)
            except Exception as e:
                stub_warnings.append(f"{module_name}: failed to emit stub: {e!r}")
        return emitted_stubs, stub_warnings

    if not stale:
        emitted_stubs, stub_warnings = _emit_stubs(set(), skipped)
        return BuildReport(
            generated=set(),
            skipped=skipped,
            failed=skipped_failures,
            emitted_stubs=emitted_stubs,
            stub_warnings=stub_warnings,
        )

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
    module_needs_deps: dict[str, list[str]] = {}
    module_context_stats: dict[str, dict[str, dict[str, int]]] = {}
    from jaunt.generate.shared import load_prompt as _load_prompt

    _preamble_chars = len(_load_prompt("codex_preamble.md", None))
    _skills_ws_chars = _skills_workspace_chars(project_root)
    failed: dict[str, list[str]] = dict(skipped_failures)
    completed: set[str] = set()
    ty_cmd = _resolve_ty_cmd() if ty_attempts is not None else None
    llm_slots = asyncio.Semaphore(jobs)

    def _phase(module_name: str, stage: str, detail: str = "") -> None:
        if progress is None:
            return
        phase = getattr(progress, "phase", None)
        if callable(phase):
            try:
                phase(module_name, stage, detail)
            except Exception:
                pass

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
                    _phase(module_name, "cache hit")
                    if cost_tracker is not None:
                        cost_tracker.record_cache_hit()

        if result_source is None:
            max_attempts = (2 + (ty_attempts or 0)) if ty_cmd is not None else 2
            async with llm_slots:
                _phase(module_name, "generating")
                result = await backend.generate_with_retry(
                    ctx,
                    max_attempts=max_attempts,
                    extra_validator=retry_validator,
                    initial_error_context=(initial_error_context_by_module or {}).get(module_name),
                    progress=lambda stage, detail: _phase(module_name, stage, detail),
                )
            if result.source is None:
                return False, None, result.errors or ["No source returned."]
            if result.errors:
                return False, None, result.errors

            result_source = result.source
            _phase(module_name, "validating")
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

            wcc = _whole_class_context(
                component_entries,
                specs=specs,
                package_dir=package_dir,
                generated_dir=generated_dir,
            )
            component_contract = build_module_context_artifacts(
                module_name=module_name,
                entries=entries,
                expected_names=component_expected,
                generated_names=all_generated_names,
                module_specs=module_specs,
                module_dag=module_dag,
                package_dir=package_dir,
                generated_dir=generated_dir,
                build_instructions=build_instructions,
                targeted_test_entries=targeted_test_entries,
                base_contract_block=wcc.base_contract_block,
                whole_class_contract_block=wcc.whole_class_contract_block,
                inherited_api_block=wcc.inherited_api_block,
            )
            whole = _whole_class_specs(component_entries)
            seed_target_content = ""
            whole_class_contract_block = wcc.whole_class_contract_block
            if whole:
                from jaunt.class_analysis import (
                    build_class_scaffold,
                    collect_spec_module_imports,
                )

                spec_src = Path(component_entries[0].source_file).read_text(encoding="utf-8")
                imports = collect_spec_module_imports(spec_src)
                scaffolds = [
                    build_class_scaffold(extract_source_segment(e)) for e in whole.values()
                ]
                seed_parts: list[str] = []
                if imports:
                    seed_parts.append("\n".join(imports))
                seed_parts.extend(scaffolds)
                seed_target_content = "\n\n\n".join(seed_parts).rstrip() + "\n"
            relevant_block = ""
            relevant_files: tuple[tuple[str, str], ...] = ()
            if search_enabled:
                from jaunt.repo_context import search as rc_search

                query_text = (
                    " ".join(component_expected) + " " + " ".join(decorator_prompts.values())
                )
                hits = rc_search.query(query_text, root=package_dir, max_hits=search_max_hits)
                if hits:
                    relevant_files = tuple(
                        (f"relevant_{i}.py", f"# {h.file}\n{h.snippet}\n")
                        for i, h in enumerate(hits)
                    )
                    relevant_block = rc_search.render_relevant_block(list(hits))
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
                repo_map_block=repo_map_block,
                project_overview_block=project_overview_block,
                relevant_context_block=relevant_block,
                relevant_context_files=relevant_files,
                module_contract_block=component_contract.module_contract_block,
                base_contract_block=component_contract.base_contract_block,
                blueprint_source=component_contract.blueprint_source,
                build_instructions_block=component_contract.build_instructions_block,
                attached_test_specs_block=component_contract.attached_test_specs_block,
                package_context_block=component_contract.package_context_block,
                module_context_digest=component_contract.digest,
                async_runner=async_runner,
                project_root=project_root,
                builtin_skill_names=tuple(builtin_skill_names),
                skills_digest=skills_digest,
                seed_target_content=seed_target_content,
                whole_class_contract_block=whole_class_contract_block,
                whole_class=bool(whole),
            )
            return ctx, tuple(component_expected), component_contract.handwritten_names

        def _make_validators(
            *,
            component_entries: list[SpecEntry],
            component_expected: list[str],
            handwritten_names: tuple[str, ...],
        ) -> tuple[Callable[[str], list[str]], Callable[[str], list[str]]]:
            generated_module = paths.spec_module_to_generated_module(
                module_name, generated_dir=generated_dir
            )

            def _validate_imports(source: str) -> list[str]:
                if not check_generated_imports:
                    return []
                return validate_build_generated_source(
                    source,
                    [],
                    spec_module=module_name,
                    handwritten_names=(),
                    generated_module=generated_module,
                    project_dir=package_dir,
                    source_roots=validation_source_roots,
                    first_party_modules=first_party_modules,
                    check_imports=True,
                    import_allowlist=generated_import_allowlist,
                )

            def _validate_candidate(source: str) -> list[str]:
                errs = validate_build_generated_source(
                    source,
                    component_expected,
                    spec_module=module_name,
                    handwritten_names=handwritten_names,
                    generated_module=generated_module,
                    project_dir=package_dir,
                    source_roots=validation_source_roots,
                    first_party_modules=first_party_modules,
                    check_imports=check_generated_imports,
                    import_allowlist=generated_import_allowlist,
                )
                if errs:
                    return errs
                whole = _whole_class_specs(component_entries)
                for entry in whole.values():
                    kw = _class_validation_inputs(entry)
                    class_errs = validate_build_class_source(source, **kw)  # type: ignore[arg-type]
                    if class_errs:
                        return class_errs
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
                errs = _validate_imports(source)
                if errs:
                    return errs
                whole = _whole_class_specs(component_entries)
                for entry in whole.values():
                    kw = _class_validation_inputs(entry)
                    class_errs = validate_build_class_source(source, **kw)  # type: ignore[arg-type]
                    if class_errs:
                        return class_errs
                if ty_validator is None:
                    return []
                return ty_validator(source)

            return _validate_candidate, _retry_validator

        wcc_module = _whole_class_context(
            entries,
            specs=specs,
            package_dir=package_dir,
            generated_dir=generated_dir,
        )
        module_contract = build_module_context_artifacts(
            module_name=module_name,
            entries=entries,
            expected_names=expected,
            generated_names=all_generated_names,
            module_specs=module_specs,
            module_dag=module_dag,
            package_dir=package_dir,
            generated_dir=generated_dir,
            build_instructions=build_instructions,
            targeted_test_entries=targeted_test_entries,
            base_contract_block=wcc_module.base_contract_block,
            whole_class_contract_block=wcc_module.whole_class_contract_block,
            inherited_api_block=wcc_module.inherited_api_block,
        )
        handwritten_names = module_contract.handwritten_names

        def _validate_module_candidate(source: str) -> list[str]:
            errs = validate_build_generated_source(
                source,
                expected,
                spec_module=module_name,
                handwritten_names=handwritten_names,
                generated_module=paths.spec_module_to_generated_module(
                    module_name, generated_dir=generated_dir
                ),
                project_dir=package_dir,
                source_roots=validation_source_roots,
                first_party_modules=first_party_modules,
                check_imports=check_generated_imports,
                import_allowlist=generated_import_allowlist,
            )
            if errs:
                return errs
            whole = _whole_class_specs(entries)
            for entry in whole.values():
                kw = _class_validation_inputs(entry)
                class_errs = validate_build_class_source(source, **kw)  # type: ignore[arg-type]
                if class_errs:
                    return class_errs
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
                    component_entries=component_entries,
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
                component_entries=entries,
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

        for entry in _whole_class_specs(entries).values():
            kw = _class_warning_inputs(entry)
            for warning in class_build_warnings(result_source, **kw):  # type: ignore[arg-type]
                _phase(module_name, "warning", warning)

        generated_sources[module_name] = result_source
        markers = _scan_needs_dep_markers(result_source)
        if markers:
            module_needs_deps[module_name] = markers
        module_context_stats[module_name] = _module_context_stats(
            artifacts=module_contract,
            whole_class_contract_block=wcc_module.whole_class_contract_block,
            dep_apis=dep_apis,
            dep_gen=dep_gen,
            repo_map_block=repo_map_block,
            project_overview_block=project_overview_block,
            preamble_chars=_preamble_chars,
            skills_workspace_chars=_skills_ws_chars,
        )

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
        if wcc_module.base_api_digest:
            header_fields["base_api_digest"] = wcc_module.base_api_digest
        spec_digests = _compute_spec_digests(entries)
        snapshots = _compute_snapshots(entries)

        write_generated_module(
            package_dir=package_dir,
            generated_dir=generated_dir,
            module_name=module_name,
            source=result_source,
            header_fields=header_fields,
            spec_digests=spec_digests,
            snapshots=snapshots,
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

    emitted_stubs, stub_warnings = _emit_stubs(generated, skipped)
    return BuildReport(
        generated=generated,
        skipped=skipped,
        failed=failed,
        needs_deps=module_needs_deps,
        context_stats=module_context_stats,
        emitted_stubs=emitted_stubs,
        stub_warnings=stub_warnings,
    )
