"""Hear the unwritten module speak.

This module performs the first, intentionally pure pass for module-level magic:
it scans an AST for top-level stubs that can be governed by Jaunt without
touching registries, importing user modules, or doing file-system work.
"""

from __future__ import annotations

import ast
import inspect
import sys
import types
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jaunt.class_analysis import is_stub_body
from jaunt.errors import JauntError
from jaunt.registry import (
    ModuleMagicDefaults,
    SpecEntry,
    get_module_magic_defaults,
    register_magic,
    register_module_magic,
)
from jaunt.spec_ref import SpecRef, normalize_spec_ref


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


_MISSING: object = object()


@dataclass(frozen=True, slots=True)
class _ModuleMagicState:
    module: str
    spec_names: frozenset[str]
    class_names: frozenset[str]


class _MagicModule(types.ModuleType):
    """A governed module that forwards its stubs to generated code on first access.

    Approach A: intercept once, rebind every governed name in the module dict, then
    swap ``__class__`` back to :class:`types.ModuleType` so steady-state attribute
    access pays zero interception cost.
    """

    def __getattribute__(self, attr: str) -> object:
        d = object.__getattribute__(self, "__dict__")
        state = d.get("__jaunt_magic_module__")
        if state is None or attr not in state.spec_names:
            return types.ModuleType.__getattribute__(self, attr)
        if any(name not in d for name in state.spec_names):
            # Module still executing / circular import: bypass resolution.
            return types.ModuleType.__getattribute__(self, attr)
        _resolve_module(self, state, d)
        return types.ModuleType.__getattribute__(self, attr)


def _not_built_binding(name: str, module: str, spec_ref: SpecRef, *, is_class: bool) -> object:
    from jaunt.runtime import _not_built_error

    if is_class:

        def __new__(cls, *args: Any, **kwargs: Any):  # noqa: ANN001
            raise _not_built_error(spec_ref)

        return type(name, (), {"__module__": module, "__qualname__": name, "__new__": __new__})

    def _raiser(*args: Any, **kwargs: Any) -> object:
        raise _not_built_error(spec_ref)

    _raiser.__name__ = name
    _raiser.__qualname__ = name
    _raiser.__module__ = module
    return _raiser


def _resolve_module(mod: types.ModuleType, state: _ModuleMagicState, d: dict[str, object]) -> None:
    from jaunt.runtime import _import_generated_module

    d.setdefault("__jaunt_original_stubs__", {n: d[n] for n in state.spec_names})

    try:
        gen_mod: types.ModuleType | None = _import_generated_module(state.module)
    except ModuleNotFoundError:
        gen_mod = None

    for name in state.spec_names:
        spec_ref = normalize_spec_ref(f"{state.module}:{name}")
        is_class = name in state.class_names
        gen = _MISSING if gen_mod is None else getattr(gen_mod, name, _MISSING)
        if gen is _MISSING:
            d[name] = _not_built_binding(name, state.module, spec_ref, is_class=is_class)
            continue
        if is_class and isinstance(gen, type):
            cast(Any, gen).__jaunt_spec_ref__ = f"{state.module}:{name}"
            gen.__module__ = state.module
        d[name] = gen

    mod.__class__ = types.ModuleType


def magic_module(
    name: str,
    *,
    deps: object | None = None,
    prompt: object | None = None,
    infer_deps: object | None = None,
    test: object | None = None,
) -> None:
    """Activate module-level magic for the calling module.

    Every top-level stub (see :func:`scan_module_source`) becomes a ``@jaunt.magic``
    spec without a per-symbol decorator. Call it once, at module top level, above the
    definitions it governs::

        import jaunt

        jaunt.magic_module(__name__, prompt="All parsers are RFC 5322 strict.")
    """
    frame = inspect.currentframe()
    caller = frame.f_back if frame is not None else None
    if caller is None or caller.f_code.co_name != "<module>":
        raise JauntError(
            "jaunt.magic_module(...) must be called at module top level, "
            "before the definitions it governs."
        )

    if name not in sys.modules:
        raise JauntError(
            f"magic_module({name!r}): no such module in sys.modules; "
            "pass __name__ from the module being governed."
        )
    if get_module_magic_defaults(name) is not None:
        raise JauntError(
            f"magic_module() was already called for module {name!r}; one governing call per module."
        )

    decorator_kwargs: dict[str, object] = {}
    if deps is not None:
        decorator_kwargs["deps"] = deps
    if prompt is not None:
        decorator_kwargs["prompt"] = prompt
    if infer_deps is not None:
        decorator_kwargs["infer_deps"] = infer_deps
    if test is not None:
        if not isinstance(test, bool):
            raise JauntError("magic_module(test=...) must be a boolean when provided.")
        decorator_kwargs["test"] = test

    mod = sys.modules[name]
    source_file = getattr(mod, "__file__", None)
    if not isinstance(source_file, str) or not source_file:
        raise JauntError(f"magic_module({name!r}): module has no source file on disk to scan.")

    tree = ast.parse(Path(source_file).read_text(encoding="utf-8"))
    scan = scan_module_source(tree, module=name)

    register_module_magic(
        ModuleMagicDefaults(module=name, source_file=source_file, decorator_kwargs=decorator_kwargs)
    )

    if not scan.candidates:
        warnings.warn(
            f"magic_module({name!r}): no top-level stubs classified as specs "
            "(all bodies are real or decorated). Legal during gradual conversion; "
            "check placement if unexpected.",
            UserWarning,
            stacklevel=2,
        )
        return

    spec_names: set[str] = set()
    class_names: set[str] = set()
    for candidate in scan.candidates:
        register_magic(
            SpecEntry(
                kind="magic",
                spec_ref=normalize_spec_ref(f"{name}:{candidate.name}"),
                module=name,
                qualname=candidate.name,
                source_file=source_file,
                obj=None,
                decorator_kwargs=dict(decorator_kwargs),
                class_name=None,
                origin="module",
            )
        )
        spec_names.add(candidate.name)
        if candidate.is_class:
            class_names.add(candidate.name)

    for message in scan.warnings:
        warnings.warn(message, UserWarning, stacklevel=2)

    mod.__dict__["__jaunt_magic_module__"] = _ModuleMagicState(
        module=name,
        spec_names=frozenset(spec_names),
        class_names=frozenset(class_names),
    )
    mod.__class__ = _MagicModule
