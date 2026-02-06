from __future__ import annotations

import ast


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


def compile_check(source: str, filename: str) -> list[str]:
    """Attempt to compile source for syntax-level errors (empty list means ok)."""

    try:
        compile(source or "", filename, "exec")
    except SyntaxError as e:
        return [_syntax_error_to_str(e)]
    except Exception as e:  # pragma: no cover - rare, but return a friendly string.
        return [f"CompileError: {e!r}"]
    return []
