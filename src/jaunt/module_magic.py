"""Hear the unwritten module speak.

This module performs the first, intentionally pure pass for module-level magic:
it scans an AST for top-level stubs that can be governed by Jaunt without
touching registries, importing user modules, or doing file-system work.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from jaunt.class_analysis import is_stub_body


_JAUNT_MEMBER_NAMES = frozenset({"magic", "sig", "preserve", "test", "contract"})


@dataclass(frozen=True, slots=True)
class ModuleSpecCandidate:
    name: str
    is_class: bool


@dataclass(frozen=True, slots=True)
class ModuleScan:
    candidates: tuple[ModuleSpecCandidate, ...]
    warnings: tuple[str, ...]


def _jaunt_decorator_aliases(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    module_aliases: set[str] = set()
    member_aliases: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "jaunt":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module in {"jaunt", "jaunt.runtime"}:
                for alias in node.names:
                    if alias.name in _JAUNT_MEMBER_NAMES:
                        member_aliases.add(alias.asname or alias.name)

    return frozenset(module_aliases), frozenset(member_aliases)


def _matches_jaunt_decorator(
    dec: ast.expr,
    module_aliases: frozenset[str],
    member_aliases: frozenset[str],
    members: frozenset[str] = frozenset({"magic", "sig", "preserve", "test", "contract"}),
) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id in module_aliases
            and target.attr in members
        )
    if isinstance(target, ast.Name):
        return target.id in member_aliases
    return False


def _preserve_aliases(tree: ast.Module) -> frozenset[str]:
    aliases: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level != 0:
            continue
        if node.module not in {"jaunt", "jaunt.runtime"}:
            continue
        for alias in node.names:
            if alias.name == "preserve":
                aliases.add(alias.asname or alias.name)
    return frozenset(aliases)


def _matches_preserve_decorator(
    dec: ast.expr,
    module_aliases: frozenset[str],
    preserve_aliases: frozenset[str],
) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id in module_aliases
            and target.attr == "preserve"
        )
    if isinstance(target, ast.Name):
        return target.id in preserve_aliases
    return False


def _is_docstring_only_class(node: ast.ClassDef) -> bool:
    return all(
        isinstance(stmt, ast.Pass)
        or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        for stmt in node.body
    )


def _is_unpreserved_stub_method(
    node: ast.stmt,
    module_aliases: frozenset[str],
    preserve_aliases: frozenset[str],
) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if any(
        _matches_preserve_decorator(dec, module_aliases, preserve_aliases)
        for dec in node.decorator_list
    ):
        return False
    return is_stub_body(node)


def _value_for_warning(node: ast.stmt) -> ast.expr | None:
    if isinstance(node, (ast.Expr, ast.Assign)):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _called_spec_names(value: ast.expr, spec_names: frozenset[str]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for child in ast.walk(value):
        if not isinstance(child, ast.Call):
            continue
        if not isinstance(child.func, ast.Name):
            continue
        name = child.func.id
        if name not in spec_names or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return tuple(names)


def scan_module_source(tree: ast.Module, *, module: str) -> ModuleScan:
    module_aliases, member_aliases = _jaunt_decorator_aliases(tree)
    preserve_aliases = _preserve_aliases(tree)
    candidates: list[ModuleSpecCandidate] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(
                _matches_jaunt_decorator(dec, module_aliases, member_aliases)
                for dec in node.decorator_list
            ):
                continue
            if node.decorator_list:
                continue
            if is_stub_body(node):
                candidates.append(ModuleSpecCandidate(name=node.name, is_class=False))
        elif isinstance(node, ast.ClassDef):
            if any(
                _matches_jaunt_decorator(dec, module_aliases, member_aliases)
                for dec in node.decorator_list
            ):
                continue
            if node.decorator_list:
                continue
            if _is_docstring_only_class(node) or any(
                _is_unpreserved_stub_method(member, module_aliases, preserve_aliases)
                for member in node.body
            ):
                candidates.append(ModuleSpecCandidate(name=node.name, is_class=True))

    spec_names = frozenset(candidate.name for candidate in candidates)
    warnings: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id in spec_names:
                    message = (
                        f"{module}: class '{node.name}' subclasses governed spec '{base.id}' "
                        "at module level; it will see the pre-rebind stub. Move the subclass "
                        f"into a function or mark '{base.id}' with an explicit @jaunt.magic."
                    )
                    warnings.append((node.lineno, message))
                    break

        value = _value_for_warning(node)
        if value is None:
            continue
        for name in _called_spec_names(value, spec_names):
            message = (
                f"{module}: module-level code calls governed spec '{name}' before rebinding; "
                "it will see the pre-rebind stub. Move the call into a function."
            )
            warnings.append((node.lineno, message))

    return ModuleScan(
        candidates=tuple(candidates),
        warnings=tuple(message for _, message in sorted(warnings)),
    )
