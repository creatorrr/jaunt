"""Static analysis of a @magic class body: modes, stub heuristic, member split."""

from __future__ import annotations

import ast
import inspect
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


@dataclass(frozen=True, slots=True)
class BaseContract:
    block: str
    project_base_refs: tuple[str, ...]
    required_abstractmethods: tuple[str, ...]


def resolve_base_contract(cls_obj: type) -> BaseContract:
    required = tuple(sorted(getattr(cls_obj, "__abstractmethods__", frozenset())))

    project_refs: list[str] = []
    for base in cls_obj.__bases__:
        if base is object:
            continue
        mod = getattr(base, "__module__", "")
        qual = getattr(base, "__qualname__", base.__name__)
        # A project base is any non-stdlib base; record a spec-ref-shaped string.
        if mod and not mod.startswith(("builtins", "abc", "typing", "collections")):
            project_refs.append(f"{mod}:{qual}")

    lines: list[str] = []
    seen: set[str] = set()
    for base in cls_obj.__mro__[1:]:
        if base is object:
            continue
        for name, member in sorted(vars(base).items()):
            if name.startswith("_") and not name.startswith("__"):
                continue
            if name in seen or not callable(member):
                continue
            seen.add(name)
            try:
                sig = str(inspect.signature(member))
            except (TypeError, ValueError):
                sig = "(...)"
            abstract = " [abstractmethod]" if name in required else ""
            lines.append(f"{base.__name__}.{name}{sig}{abstract}")

    block = "\n".join(lines) if lines else "(no base classes)"
    return BaseContract(
        block=block,
        project_base_refs=tuple(project_refs),
        required_abstractmethods=required,
    )
