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


async def enrich(
    items: list[tuple[str, Path]],
    *,
    backend,
    ast_descriptions: dict[str, str],
    head_lines: int = 40,
    max_len: int = 100,
) -> dict[str, str]:
    """Batched one-line enrichment. Falls back to ast_descriptions on any failure.

    `backend` must expose `async complete_json(prompt) -> dict[path, str]`.
    """
    result = dict(ast_descriptions)
    if not items:
        return result
    parts: list[str] = [
        "For each file below, return STRICT JSON mapping the exact path to a single "
        "concise one-line description (<= 100 chars) of what the file does. "
        "Return only the JSON object.\n"
    ]
    for rel, path in items:
        try:
            head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:head_lines])
        except OSError:
            head = ""
        parts.append(
            f"### {rel}\nAST summary: {ast_descriptions.get(rel, '')}\n```python\n{head}\n```\n"
        )
    try:
        raw = await backend.complete_json("\n".join(parts))
    except Exception:  # noqa: BLE001 - any failure -> AST baseline
        return result
    if not isinstance(raw, dict):
        return result
    for rel, _ in items:
        val = raw.get(rel)
        if isinstance(val, str) and val.strip():
            result[rel] = _cap(val, max_len)
    return result
