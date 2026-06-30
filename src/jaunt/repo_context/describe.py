"""Deterministic AST-based one-line descriptions (the always-on baseline)."""

from __future__ import annotations

import ast
from pathlib import Path

_FALLBACK = "Python module"


def _first_doc_line(tree: ast.Module) -> str | None:
    doc = ast.get_docstring(tree, clean=True)
    if not doc:
        return None
    for line in doc.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _public_surface(tree: ast.Module) -> str | None:
    names: list[str] = []
    decorated_magic = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("_"):
                continue
            names.append(node.name)
            for dec in node.decorator_list:
                src = ast.unparse(dec)
                if "jaunt.magic" in src or "jaunt.test" in src or "jaunt.contract" in src:
                    decorated_magic = True
    if not names:
        return None
    prefix = "specs: " if decorated_magic else "defines "
    return prefix + ", ".join(names[:6])


def _cap(text: str, max_len: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def ast_describe(path: Path, *, max_len: int = 100) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError, ValueError):
        return _FALLBACK
    return _cap(_first_doc_line(tree) or _public_surface(tree) or _FALLBACK, max_len)


def describe_dir(path: Path, *, max_len: int = 100) -> str:
    init = path / "__init__.py"
    if init.exists():
        desc = ast_describe(init, max_len=max_len)
        if desc != _FALLBACK:
            return desc
    children = sorted(p.stem for p in path.glob("*.py") if not p.name.startswith("_"))
    if children:
        return _cap("package: " + ", ".join(children[:6]), max_len)
    return _cap(f"{path.name} package", max_len)
