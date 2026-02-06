"""Dependency graph helpers.

This module builds a spec-level dependency graph from decorator metadata and
optionally infers additional edges from best-effort AST analysis.

Graph representation convention:
- A graph is a dict[node, set[dep_nodes]] (edges point to dependencies).
- Toposort returns a list where dependencies come before dependents.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from jaunt.errors import JauntDependencyCycleError
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef, normalize_spec_ref, spec_ref_from_object

if TYPE_CHECKING:  # pragma: no cover
    from jaunt.parse_cache import ParseCache

K = TypeVar("K")


def _iter_deps_value(value: object) -> Iterable[object]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return value
    return (value,)


def _normalize_dep(dep: object) -> SpecRef | None:
    try:
        # SpecRef is a NewType over str, so treat it as a str at runtime.
        if isinstance(dep, str):
            return normalize_spec_ref(dep)
        return spec_ref_from_object(dep)
    except Exception:
        # Best-effort: ignore anything we cannot normalize.
        return None


@dataclass(frozen=True, slots=True)
class _ModuleParse:
    source: str
    tree: ast.Module
    import_aliases: dict[str, str]
    from_imports: dict[str, str]


def _parse_module_once(
    path: str,
    *,
    cache: dict[str, _ModuleParse],
    persistent_cache: ParseCache | None = None,
) -> _ModuleParse | None:
    if path in cache:
        return cache[path]

    try:
        if persistent_cache is not None:
            result = persistent_cache.parse(path)
            if result is None:
                return None
            src, tree = result
        else:
            src = Path(path).read_text(encoding="utf-8")
            tree = ast.parse(src, filename=path)
    except Exception:
        return None

    import_aliases: dict[str, str] = {}
    from_imports: dict[str, str] = {}

    # Best-effort import tracking for simple name and attribute resolution.
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                import_aliases[bound] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if not node.module:
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                # Store "pkg.mod:Name" style for easy comparison with SpecRef.
                from_imports[bound] = f"{node.module}:{alias.name}"

    parsed = _ModuleParse(
        source=src,
        tree=tree,
        import_aliases=import_aliases,
        from_imports=from_imports,
    )
    cache[path] = parsed
    return parsed


def _find_top_level_node(module: ast.Module, *, name: str) -> ast.AST | None:
    for node in module.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            return node
    return None


def _find_nested_node(tree: ast.Module, qualname: str) -> ast.AST | None:
    """Walk the AST following a dotted qualname like ``Outer.method``."""
    parts = qualname.split(".")
    node: ast.AST = tree
    for part in parts:
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            return None
        found = False
        for child in body:
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and child.name == part
            ):
                node = child
                found = True
                break
        if not found:
            return None
    return node


def _resolve_attr_chain(node: ast.Attribute) -> tuple[str, list[str]] | None:
    """Walk an attribute chain and return ``(root_name, [attr1, attr2, ...])``."""
    attrs = [node.attr]
    current = node.value
    while isinstance(current, ast.Attribute):
        attrs.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        return current.id, list(reversed(attrs))
    return None


class _NameUseCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.attr_roots: set[tuple[str, str]] = set()
        self.attr_chains: list[tuple[str, list[str]]] = []

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802 - ast API
        self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802 - ast API
        chain = _resolve_attr_chain(node)
        if chain is not None:
            root, attrs = chain
            if len(attrs) == 1:
                self.attr_roots.add((root, attrs[0]))
            else:
                self.attr_chains.append((root, attrs))
        self.generic_visit(node)


def _resolve_reexport(
    module: str,
    name: str,
    *,
    source_roots: list[Path],
    cache: dict[str, _ModuleParse],
    persistent_cache: ParseCache | None = None,
) -> SpecRef | None:
    """Follow one level of re-export through a package's ``__init__.py``."""
    parts = module.split(".")
    for root in source_roots:
        init_path = root / Path(*parts) / "__init__.py"
        if init_path.is_file():
            parsed = _parse_module_once(
                str(init_path), cache=cache, persistent_cache=persistent_cache
            )
            if parsed is not None and name in parsed.from_imports:
                try:
                    return normalize_spec_ref(parsed.from_imports[name])
                except ValueError:
                    pass
    return None


def build_spec_graph(
    specs: dict[SpecRef, SpecEntry],
    *,
    infer_default: bool,
    warnings: list[str] | None = None,
    persistent_cache: ParseCache | None = None,
    source_roots: list[Path] | None = None,
) -> dict[SpecRef, set[SpecRef]]:
    """Build a dependency graph keyed by SpecRef.

    Explicit deps:
    - read from entry.decorator_kwargs["deps"]
    - normalize values to SpecRef; ignore unknown/missing deps (MVP)

    Inference (best-effort):
    - controlled by entry.decorator_kwargs["infer_deps"] override or infer_default
    - never raises for ordinary code; failures are treated as "no inferred deps"
    """

    graph: dict[SpecRef, set[SpecRef]] = {sr: set() for sr in specs}

    parse_cache: dict[str, _ModuleParse] = {}

    for spec_ref, entry in specs.items():
        deps_out = graph.setdefault(spec_ref, set())

        explicit = entry.decorator_kwargs.get("deps")
        for dep in _iter_deps_value(explicit):
            dep_ref = _normalize_dep(dep)
            if dep_ref is None or dep_ref == spec_ref:
                continue
            if dep_ref in specs:
                deps_out.add(dep_ref)

        infer_override = entry.decorator_kwargs.get("infer_deps")
        if infer_override is None:
            infer_enabled = infer_default
        else:
            infer_enabled = bool(infer_override)

        if not infer_enabled:
            continue

        # Inference is strictly best-effort; never fail the build because it
        # cannot parse or resolve names.
        try:
            parsed = _parse_module_once(
                entry.source_file, cache=parse_cache, persistent_cache=persistent_cache
            )
            if parsed is None:
                continue

            if "." in entry.qualname:
                node = _find_nested_node(parsed.tree, entry.qualname)
            else:
                node = _find_top_level_node(parsed.tree, name=entry.qualname)
            if node is None:
                continue

            collector = _NameUseCollector()
            collector.visit(node)

            inferred: set[SpecRef] = set()
            resolved_names: set[str] = set()

            # 1) from-import direct names: `from x import Foo as Bar` then `Bar(...)`.
            for name in collector.names:
                dep = parsed.from_imports.get(name)
                if dep is not None:
                    dep_ref = normalize_spec_ref(dep)
                    if dep_ref in specs and dep_ref != spec_ref:
                        inferred.add(dep_ref)
                        resolved_names.add(name)
                    elif source_roots and dep_ref not in specs:
                        # Fallback: follow one level of re-export.
                        dep_module = str(dep_ref).split(":", 1)[0]
                        dep_name = str(dep_ref).split(":", 1)[1] if ":" in str(dep_ref) else name
                        reexport = _resolve_reexport(
                            dep_module,
                            dep_name,
                            source_roots=source_roots,
                            cache=parse_cache,
                            persistent_cache=persistent_cache,
                        )
                        if reexport is not None and reexport in specs and reexport != spec_ref:
                            inferred.add(reexport)
                            resolved_names.add(name)

            # 2) bare names: try same-module resolution (common for sibling defs/classes).
            for name in collector.names:
                candidate = SpecRef(f"{entry.module}:{name}")
                if candidate in specs and candidate != spec_ref:
                    inferred.add(candidate)
                    resolved_names.add(name)

            # 3) attribute roots: `alias.Foo` where alias comes from `import pkg.mod as alias`.
            for root, attr in collector.attr_roots:
                mod = parsed.import_aliases.get(root)
                if not mod:
                    continue
                candidate = normalize_spec_ref(f"{mod}:{attr}")
                if candidate in specs and candidate != spec_ref:
                    inferred.add(candidate)

            # 4) multi-level attribute chains: `alias.sub.Foo` where alias maps
            #    to a package.  Try progressively: mod.sub:Foo, mod:sub.Foo.
            for root, attrs in collector.attr_chains:
                mod = parsed.import_aliases.get(root)
                if not mod:
                    continue
                # For chain [sub, inner, Foo] with mod="pkg", try:
                #   pkg.sub.inner:Foo, pkg.sub:inner.Foo, pkg:sub.inner.Foo
                for split in range(len(attrs) - 1, -1, -1):
                    mod_part = ".".join([mod, *attrs[:split]]) if split else mod
                    qual_part = ".".join(attrs[split:])
                    try:
                        candidate = normalize_spec_ref(f"{mod_part}:{qual_part}")
                    except ValueError:
                        continue
                    if candidate in specs and candidate != spec_ref:
                        inferred.add(candidate)
                        break

            # Collect warnings for from-import names that are used but couldn't
            # be resolved to any known spec.
            if warnings is not None:
                for name in collector.names:
                    if name in resolved_names:
                        continue
                    ref = parsed.from_imports.get(name)
                    if ref is not None:
                        warnings.append(
                            f"unresolved inferred dep: {spec_ref!s} uses '{name}' "
                            f"(from import {ref}) but it is not a known spec"
                        )

            deps_out.update(inferred)
        except Exception:
            continue

    return graph


def collapse_to_module_dag(spec_graph: dict[SpecRef, set[SpecRef]]) -> dict[str, set[str]]:
    """Collapse a spec dependency graph into a module-level graph."""

    module_graph: dict[str, set[str]] = {}

    def mod_of(sr: SpecRef) -> str:
        return str(sr).split(":", 1)[0]

    for sr, deps in spec_graph.items():
        m = mod_of(sr)
        module_graph.setdefault(m, set())
        for dep in deps:
            dm = mod_of(dep)
            module_graph.setdefault(dm, set())
            if dm != m:
                module_graph[m].add(dm)

    return module_graph


def find_cycles(graph: dict[K, set[K]]) -> list[list[K]]:
    """Return all distinct elementary cycles in *graph*, or ``[]`` if acyclic.

    Uses a DFS-based approach: when a back-edge is found during traversal, the
    cycle path is extracted from the recursion stack.  Each cycle is reported
    once (normalized so the lexicographically smallest element comes first).
    """

    all_nodes: set[K] = set(graph.keys())
    for deps in graph.values():
        all_nodes.update(deps)

    perm: set[K] = set()
    temp: set[K] = set()
    stack: list[K] = []
    seen_cycles: set[tuple[K, ...]] = set()
    cycles: list[list[K]] = []

    def visit(n: K) -> None:
        if n in perm:
            return
        if n in temp:
            try:
                i = stack.index(n)
            except ValueError:
                return
            cycle = stack[i:]
            # Normalize: rotate so smallest element is first.
            min_idx = min(range(len(cycle)), key=lambda i: str(cycle[i]))
            normalized = tuple(cycle[min_idx:] + cycle[:min_idx])
            if normalized not in seen_cycles:
                seen_cycles.add(normalized)
                cycles.append(list(normalized))
            return

        temp.add(n)
        stack.append(n)
        for dep in sorted(graph.get(n, set()), key=lambda x: str(x)):
            visit(dep)
        stack.pop()
        temp.remove(n)
        perm.add(n)

    for node in sorted(all_nodes, key=lambda x: str(x)):
        if node not in perm:
            visit(node)

    return cycles


def toposort(graph: dict[K, set[K]]) -> list[K]:
    """Topologically sort a dependency graph (deps before dependents)."""

    perm: set[K] = set()
    temp: set[K] = set()
    order: list[K] = []
    stack: list[K] = []

    def visit(n: K) -> None:
        if n in perm:
            return
        if n in temp:
            # Extract a cycle path from the current recursion stack.
            try:
                i = stack.index(n)
            except ValueError:
                i = 0
            cycle = stack[i:] + [n]
            msg = "Dependency cycle detected: " + " -> ".join(str(x) for x in cycle)
            raise JauntDependencyCycleError(msg)

        temp.add(n)
        stack.append(n)
        for dep in sorted(graph.get(n, set()), key=lambda x: str(x)):
            visit(dep)
        stack.pop()
        temp.remove(n)
        perm.add(n)
        order.append(n)

    # Ensure nodes that only appear as deps are also considered.
    all_nodes: set[K] = set(graph.keys())
    for deps in graph.values():
        all_nodes.update(deps)

    for node in sorted(all_nodes, key=lambda x: str(x)):
        if node not in perm:
            visit(node)

    return order
