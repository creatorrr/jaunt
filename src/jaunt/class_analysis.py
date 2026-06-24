"""Static analysis of a @magic class body: modes, stub heuristic, member split."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Literal


def is_preserve_decorator(dec: ast.expr) -> bool:
    """True for ``@jaunt.preserve``, ``@preserve``, or their called forms."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id == "jaunt"
            and target.attr == "preserve"
        )
    if isinstance(target, ast.Name):
        return target.id == "preserve"
    return False


def is_stub_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the body is only docstring / ``...`` / ``pass`` / ``raise NotImplementedError``."""
    for stmt in node.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            # docstring or a bare ``...`` (Ellipsis is a Constant in 3.8+).
            continue
        if isinstance(stmt, ast.Raise) and _is_not_implemented(stmt):
            continue
        return False
    return True


def _is_not_implemented(node: ast.Raise) -> bool:
    exc = node.exc
    if exc is None:
        return False
    if isinstance(exc, ast.Name):
        return exc.id == "NotImplementedError"
    if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
        return exc.func.id == "NotImplementedError"
    return False


@dataclass(frozen=True, slots=True)
class MemberSplit:
    stubs: tuple[str, ...]
    preserved: tuple[str, ...]
    preserve_marked: tuple[str, ...]


def _iter_methods(
    class_node: ast.ClassDef,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in class_node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def split_class_members(class_node: ast.ClassDef) -> MemberSplit:
    stubs: list[str] = []
    preserved: list[str] = []
    preserve_marked: list[str] = []
    for fn in _iter_methods(class_node):
        marked = any(is_preserve_decorator(d) for d in fn.decorator_list)
        if marked:
            preserve_marked.append(fn.name)
            preserved.append(fn.name)
        elif is_stub_body(fn):
            stubs.append(fn.name)
        else:
            preserved.append(fn.name)
    return MemberSplit(
        stubs=tuple(sorted(stubs)),
        preserved=tuple(sorted(preserved)),
        preserve_marked=tuple(sorted(preserve_marked)),
    )


def classify_class_mode(class_node: ast.ClassDef) -> Literal["docstring_only", "stubs", "mix"]:
    methods = _iter_methods(class_node)
    if not methods:
        return "docstring_only"
    split = split_class_members(class_node)
    if split.stubs and not split.preserved:
        return "stubs"
    if not split.stubs and split.preserved:
        return "mix"  # all-real class under @magic is still "mix" (nothing to generate but bodies)
    return "mix"
