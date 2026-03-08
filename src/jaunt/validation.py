"""Fearful symmetry: AST validation of generated code.

Could frame thy fearful symmetry? -- verify that what the furnace produced
has the right shape.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable


def _syntax_error_to_str(err: SyntaxError) -> str:
    # Keep formatting stable and readable for retry prompts.
    lineno = getattr(err, "lineno", None)
    offset = getattr(err, "offset", None)
    loc = ""
    if lineno is not None:
        loc = f" (line {lineno}"
        if offset is not None:
            loc += f":{offset}"
        loc += ")"
    msg = getattr(err, "msg", None) or str(err) or "invalid syntax"
    return f"SyntaxError: {msg}{loc}"


def validate_generated_source(source: str, expected_names: list[str]) -> list[str]:
    """Validate generated Python source.

    Checks:
    - parses via `ast.parse` (syntax errors)
    - verifies required *top-level* names exist:
      - function defs (sync + async)
      - class defs
      - simple assignments (`NAME = ...` and `NAME: T = ...`)
    """

    if expected_names is None:
        expected_names = []

    try:
        mod = ast.parse(source or "")
    except SyntaxError as e:
        return [_syntax_error_to_str(e)]

    defined: set[str] = set()
    for node in mod.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            continue

        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    defined.add(tgt.id)
            continue

        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                defined.add(node.target.id)
            continue

    errors: list[str] = []
    for name in expected_names:
        if name not in defined:
            errors.append(f"Missing top-level definition: {name}")

    return errors


def validate_build_generated_source(
    source: str,
    expected_names: list[str],
    *,
    spec_module: str,
    handwritten_names: Iterable[str],
) -> list[str]:
    errors, mod = _base_validation(source, expected_names)
    if mod is None:
        return errors

    errors.extend(
        _validate_build_contract_only(
            mod,
            expected_names=expected_names,
            spec_module=spec_module,
            handwritten_names=handwritten_names,
        )
    )
    return errors


def validate_build_contract_only(
    source: str,
    *,
    expected_names: list[str],
    spec_module: str,
    handwritten_names: Iterable[str],
) -> list[str]:
    try:
        mod = ast.parse(source or "")
    except SyntaxError:
        return []
    return _validate_build_contract_only(
        mod,
        expected_names=expected_names,
        spec_module=spec_module,
        handwritten_names=handwritten_names,
    )


def _validate_build_contract_only(
    mod: ast.Module,
    *,
    expected_names: list[str],
    spec_module: str,
    handwritten_names: Iterable[str],
) -> list[str]:
    errors: list[str] = []
    forbidden = set(handwritten_names) - set(expected_names)
    if not forbidden:
        return errors

    for name in _defined_top_level_names(mod):
        if name in forbidden:
            errors.append(
                "Generated source must not redefine handwritten source-module symbol "
                f"{name!r}. Import or reuse {name!r} from {spec_module!r} instead."
            )
    return errors


def validate_test_generated_source(
    source: str,
    expected_names: list[str],
    *,
    spec_module: str,
    generated_module: str,
    public_api_only_by_name: dict[str, bool],
) -> list[str]:
    errors, mod = _base_validation(source, expected_names)
    if mod is None:
        return errors

    errors.extend(
        _validate_test_contract_only(
            mod,
            spec_module=spec_module,
            generated_module=generated_module,
            public_api_only_by_name=public_api_only_by_name,
        )
    )
    return errors


def validate_test_contract_only(
    source: str,
    *,
    spec_module: str,
    generated_module: str,
    public_api_only_by_name: dict[str, bool],
) -> list[str]:
    try:
        mod = ast.parse(source or "")
    except SyntaxError:
        return []
    return _validate_test_contract_only(
        mod,
        spec_module=spec_module,
        generated_module=generated_module,
        public_api_only_by_name=public_api_only_by_name,
    )


def _validate_test_contract_only(
    mod: ast.Module,
    *,
    spec_module: str,
    generated_module: str,
    public_api_only_by_name: dict[str, bool],
) -> list[str]:
    errors: list[str] = []
    defs_by_name = {
        node.name: node
        for node in mod.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for test_name, public_api_only in sorted(public_api_only_by_name.items()):
        if not public_api_only:
            continue
        node = defs_by_name.get(test_name)
        if node is None:
            continue
        errors.extend(
            _validate_public_api_only_test(
                node,
                spec_module=spec_module,
                generated_module=generated_module,
            )
        )
    return errors


def _base_validation(source: str, expected_names: list[str]) -> tuple[list[str], ast.Module | None]:
    if expected_names is None:
        expected_names = []

    try:
        mod = ast.parse(source or "")
    except SyntaxError as e:
        return [_syntax_error_to_str(e)], None

    errors: list[str] = []
    defined = _defined_top_level_names(mod)
    for name in expected_names:
        if name not in defined:
            errors.append(f"Missing top-level definition: {name}")

    return errors, mod


def _defined_top_level_names(mod: ast.Module) -> set[str]:
    defined: set[str] = set()
    for node in mod.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            continue

        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    defined.add(tgt.id)
            continue

        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                defined.add(node.target.id)
            continue
    return defined


def _validate_public_api_only_test(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    spec_module: str,
    generated_module: str,
) -> list[str]:
    errors: list[str] = []
    forbidden_dunders = {"__globals__", "__dict__", "__code__", "__closure__", "__wrapped__"}
    forbidden_modules = {
        spec_module,
        generated_module,
    }

    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom):
            module = child.module or ""
            if any(module == mod or module.startswith(mod + ".") for mod in forbidden_modules):
                errors.append(
                    f"{node.name}: public_api_only tests must not import from {module!r}."
                )
            for alias in child.names:
                if alias.name.startswith("_"):
                    errors.append(
                        f"{node.name}: public_api_only tests must not import underscore-prefixed "
                        f"symbol {alias.name!r}."
                    )

        elif isinstance(child, ast.Import):
            for alias in child.names:
                mod_name = alias.name
                if any(
                    mod_name == mod or mod_name.startswith(mod + ".") for mod in forbidden_modules
                ):
                    errors.append(
                        f"{node.name}: public_api_only tests must not import {mod_name!r}."
                    )

        elif isinstance(child, ast.Attribute):
            if child.attr in forbidden_dunders:
                errors.append(
                    f"{node.name}: public_api_only tests must not inspect {child.attr!r}."
                )
            if child.attr.startswith("_") and not child.attr.startswith("__"):
                errors.append(
                    f"{node.name}: public_api_only tests must not access underscore-prefixed "
                    f"attribute {child.attr!r}."
                )

    return errors


def compile_check(source: str, filename: str) -> list[str]:
    """Attempt to compile source for syntax-level errors (empty list means ok)."""

    try:
        compile(source or "", filename, "exec")
    except SyntaxError as e:
        return [_syntax_error_to_str(e)]
    except Exception as e:  # pragma: no cover - rare, but return a friendly string.
        return [f"CompileError: {e!r}"]
    return []
