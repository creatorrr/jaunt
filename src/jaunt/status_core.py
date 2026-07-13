"""Shared build-status computation.

The magic stale/fresh computation is used both by `jaunt status` and by the
project-aware section of `jaunt instructions`. It lives here (rather than in
`cli.py`) so the two callers cannot drift: a module the primer reports as "fresh"
is fresh by the exact same rule `jaunt status` uses.

Heavy imports (builder/discovery/registry/...) are deferred into the function so
importing this module at CLI startup stays cheap.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jaunt.config import JauntConfig
    from jaunt.registry import SpecEntry


def prepend_sys_path(dirs: Sequence[Path]) -> None:
    """Make discovered modules importable in the requested precedence order."""

    ordered = list(dict.fromkeys(str(path.resolve()) for path in dirs if path.exists()))
    if not ordered:
        return
    requested = set(ordered)
    sys.path[:] = [entry for entry in sys.path if entry not in requested]
    sys.path[:0] = ordered


def enforce_source_root_routing(
    *,
    source_dirs: Sequence[Path],
    module_specs: dict[str, list[SpecEntry]],
) -> None:
    """Compatibility no-op retained for integrations importing the 1.5 gate.

    Per-module workspace routing replaced the first-root assumption in 1.6.2.
    Route validity and duplicate module names are now checked by
    :func:`jaunt.workspace.resolve_workspace` before imports.
    """

    del source_dirs, module_specs


def iter_target_modules(targets: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for t in targets:
        mod = (t or "").split(":", 1)[0].strip()
        if mod:
            out.add(mod)
    return out


def deps_closure(modules: set[str], *, module_dag: dict[str, set[str]]) -> set[str]:
    """Return modules plus all of their dependencies (transitively)."""
    seen = set(modules)
    stack = list(modules)
    while stack:
        m = stack.pop()
        for dep in module_dag.get(m, set()):
            if dep in seen:
                continue
            seen.add(dep)
            stack.append(dep)
    return seen


def discover_targeted_test_entries(*, root: Path, cfg: JauntConfig) -> list:
    """Statically inspect test roots for `@jaunt.test(targets=...)` entries.

    Pure AST inspection (no imports, no registry mutation), so it is safe to call
    before or independently of magic discovery.
    """
    from jaunt import discovery
    from jaunt.errors import JauntDiscoveryError
    from jaunt.module_contract import extract_targeted_test_entries
    from jaunt.workspace import resolve_workspace

    workspace = resolve_workspace(root, cfg)
    entries: list = []
    for route in workspace.test_roots:
        if not route.root.exists():
            continue
        discovered = discovery.discover_module_files(
            roots=[route.root],
            exclude=[],
            generated_dir=cfg.paths.generated_dir,
            module_prefix=route.module_prefix,
        )
        for module_name, path in discovered:
            try:
                entries.extend(extract_targeted_test_entries(module_name, str(path)))
            except Exception as exc:
                raise JauntDiscoveryError(
                    f"Failed to statically inspect test module '{module_name}': "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
    return entries


@dataclass(frozen=True)
class MagicStatus:
    """Stale/fresh summary for the discovered `@jaunt.magic` modules."""

    total: int
    stale: set[str]
    fresh: set[str]
    # Per-stale-module change kind: "structural" | "prose". Empty when total == 0.
    stale_changes: dict[str, str]
    digests: dict[str, str]


def compute_magic_status(
    *,
    root: Path,
    cfg: JauntConfig,
    source_dirs: list[Path],
    build_instructions: list[str],
    include_target_tests: bool,
    infer_deps: bool,
    force: bool = False,
    target: Iterable[str] = (),
) -> MagicStatus:
    """Discover magic specs and compute which modules are stale vs fresh.

    Mirrors the computation behind `jaunt status`. Returns an empty
    ``MagicStatus`` (total 0) when no magic specs are discovered. Targeted-test
    entries are discovered internally when ``include_target_tests`` is set, so the
    staleness result matches a real build. Runs `prepend_sys_path` itself.
    """
    from jaunt import builder, discovery, registry
    from jaunt.deps import build_spec_graph, collapse_to_module_dag
    from jaunt.errors import JauntConfigError
    from jaunt.generation_fingerprint import generation_fingerprint
    from jaunt.module_api import module_api_digest
    from jaunt.module_contract import group_test_entries_by_target_module

    from jaunt.workspace import resolve_workspace

    workspace = resolve_workspace(root, cfg)
    existing = list(workspace.source_roots)
    prepend_sys_path([*existing, root])

    # Discover first, then reset the import environment through the shared entry
    # point: clearing the registries while preserving the running framework's own
    # already-imported specs. A raw clear_registries() would wipe self specs that
    # a self-package carve-out then refuses to re-register (the cached self module
    # re-import is a no-op), leaving the magic registry empty when jaunt builds
    # jaunt — the split-brain bug 1 in its status/check form.
    modules = [route.module for route in workspace.modules]
    discovery.prepare_import_environment(module_names=modules, roots=existing)
    discovery.import_and_collect(modules, kind="magic")

    specs = dict(registry.get_magic_registry())
    if not specs:
        return MagicStatus(total=0, stale=set(), fresh=set(), stale_changes={}, digests={})

    spec_graph = build_spec_graph(specs, infer_default=infer_deps)
    module_dag = collapse_to_module_dag(spec_graph)
    module_specs = registry.get_specs_by_module("magic")

    package_dir = next((d for d in existing), None)
    if package_dir is None:
        raise JauntConfigError("No existing source_roots to check.")

    build_generation_fingerprint = generation_fingerprint(
        cfg,
        kind="build",
        build_instructions=build_instructions,
        include_target_tests=include_target_tests,
    )
    build_module_context_digests: dict[str, str] = {}
    build_module_api_digests: dict[str, str] = {}
    build_module_base_api_digests: dict[str, str] = {}
    static_targeted_test_entries = (
        discover_targeted_test_entries(root=root, cfg=cfg) if include_target_tests else []
    )
    targeted_test_entries = group_test_entries_by_target_module(static_targeted_test_entries)
    for module_name, entries in module_specs.items():
        module_dir = workspace.route_for(module_name).output_base
        expected, _errs = builder._build_expected_names(entries)
        wcc = builder._whole_class_context(
            entries,
            specs=specs,
            package_dir=module_dir,
            generated_dir=cfg.paths.generated_dir,
            module_output_bases=workspace.output_bases,
        )
        build_module_context_digests[module_name] = builder.build_module_context_artifacts(
            module_name=module_name,
            entries=entries,
            expected_names=expected,
            module_specs=module_specs,
            module_dag=module_dag,
            package_dir=module_dir,
            generated_dir=cfg.paths.generated_dir,
            build_instructions=build_instructions,
            targeted_test_entries=targeted_test_entries,
            base_contract_block=wcc.base_contract_block,
            whole_class_contract_block=wcc.whole_class_contract_block,
            inherited_api_block=wcc.inherited_api_block,
        ).digest
        build_module_api_digests[module_name] = module_api_digest(entries)
        build_module_base_api_digests[module_name] = wcc.base_api_digest

    stale = builder.detect_stale_modules(
        package_dir=package_dir,
        generated_dir=cfg.paths.generated_dir,
        module_specs=module_specs,
        specs=specs,
        spec_graph=spec_graph,
        generation_fingerprint=build_generation_fingerprint,
        module_context_digests=build_module_context_digests,
        module_base_api_digests=build_module_base_api_digests,
        module_output_bases=workspace.output_bases,
        force=force,
    )
    api_changed = builder.detect_api_changed_modules(
        package_dir=package_dir,
        generated_dir=cfg.paths.generated_dir,
        module_specs=module_specs,
        module_api_digests=build_module_api_digests,
        module_output_bases=workspace.output_bases,
    )

    target_mods = iter_target_modules(target)
    if target_mods:
        allowed = deps_closure(target_mods, module_dag=module_dag)
        all_mods = {m for m in module_specs if m in allowed}
        api_changed = {m for m in api_changed if m in allowed}
    else:
        all_mods = set(module_specs.keys())

    from jaunt.digest import module_digest

    digests = {
        module_name: module_digest(module_name, module_specs[module_name], specs, spec_graph)
        for module_name in sorted(all_mods)
    }
    stale = builder.expand_stale_modules(module_dag, stale & all_mods, changed_modules=api_changed)
    fresh = all_mods - stale

    stale_changes = {
        m: _label_change_kind(
            generation_fingerprint=build_generation_fingerprint,
            module_context_digest=build_module_context_digests.get(m, ""),
            module_name=m,
            package_dir=workspace.route_for(m).output_base,
            generated_dir=cfg.paths.generated_dir,
            module_specs=module_specs,
        )
        for m in sorted(stale)
    }
    if cfg.build.emit_stubs:
        from jaunt import stub_emitter

        for module_name in sorted(fresh):
            entries = module_specs.get(module_name, [])
            if not entries:
                continue
            gen_source = builder._read_generated(
                workspace.route_for(module_name).output_base,
                cfg.paths.generated_dir,
                module_name,
            )
            if gen_source is None:
                continue
            reason = stub_emitter.stub_staleness(
                source_file=entries[0].source_file,
                generated_source=gen_source,
            )
            if reason is None:
                continue
            stale.add(module_name)
            stale_changes[module_name] = "stub"
        fresh = all_mods - stale

    return MagicStatus(
        total=len(all_mods),
        stale=stale,
        fresh=fresh,
        stale_changes=stale_changes,
        digests=digests,
    )


def _norm_digest(d: str | None) -> str | None:
    if not d:
        return None
    return d.split(":", 1)[1] if d.startswith("sha256:") else d


def _label_change_kind(
    *,
    module_name: str,
    package_dir: Path,
    generated_dir: str,
    module_specs: dict[str, list],
    generation_fingerprint: str = "",
    module_context_digest: str = "",
) -> str:
    """Classify why a stale module changed: structural, prose, fingerprint, or re-stamp.

    Structural = a signature/structure change (or never built / missing digests);
    prose = only docstring contract text changed; fingerprint = the specs are
    byte-identical but the generation fingerprint (engine, model, prompts,
    optional codex CLI version) differs — e.g. a check run in an environment
    without the codex binary while `fingerprint_cli_version` is enabled;
    re-stamp = stale but byte-identical to the stored header digests, resolvable
    by the deterministic free refreeze/re-stamp path without a model. Used by
    `jaunt status` and the semantic gate to decide whether a cheap re-freeze is possible.
    """
    from jaunt import builder
    from jaunt.digest import prose_digest, structural_digest
    from jaunt.header import (
        extract_generation_fingerprint,
        extract_module_context_digest,
        extract_spec_digests,
    )

    existing = builder._read_generated(package_dir, generated_dir, module_name)
    if existing and builder._requires_removal_restamp_rebuild(existing):
        return "structural"
    on_disk = extract_spec_digests(existing) if existing else None
    entries = module_specs.get(module_name, [])
    if not on_disk:
        return "structural"
    if set(on_disk) != {str(entry.spec_ref) for entry in entries}:
        return "structural"
    any_prose = False
    for entry in entries:
        stored = on_disk.get(str(entry.spec_ref))
        if stored is None:
            return "structural"
        if _norm_digest(stored.get("s")) != _norm_digest(structural_digest(entry)):
            return "structural"
        if _norm_digest(stored.get("p")) != _norm_digest(prose_digest(entry)):
            any_prose = True
    if any_prose:
        return "prose"
    if generation_fingerprint and existing:
        stored_fp = _norm_digest(extract_generation_fingerprint(existing))
        if stored_fp is not None and stored_fp != _norm_digest(generation_fingerprint):
            return "fingerprint"
    if module_context_digest and existing:
        stored_context = _norm_digest(extract_module_context_digest(existing))
        if stored_context != _norm_digest(module_context_digest):
            return "structural"
    return "re-stamp"
