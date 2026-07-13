"""Deterministic AST-based one-line descriptions (the always-on baseline)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_FALLBACK = "Python module"
_TYPESCRIPT_FALLBACK = "TypeScript module"
_TS_EXPORT = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?"
    r"(?:function|class|interface|type|enum|const|let)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


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


def _typescript_doc_line(source: str) -> str | None:
    match = re.match(r"^\s*/\*\*(.*?)\*/", source, re.DOTALL)
    if match is None:
        return None
    for raw in match.group(1).splitlines():
        line = raw.strip().removeprefix("*").strip()
        if line:
            return line
    return None


def _typescript_surface(path: Path, source: str) -> str | None:
    names = [match.group(1) for match in _TS_EXPORT.finditer(source)]
    names = [name for name in names if not name.startswith("_")]
    if not names:
        return None
    is_spec = ".jaunt." in path.name or any(
        marker in source for marker in ("@usejaunt/ts/spec", "jaunt.magic", "@jauntContract")
    )
    return ("specs: " if is_spec else "defines ") + ", ".join(names[:6])


def _typescript_describe(path: Path, *, max_len: int) -> str:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return _TYPESCRIPT_FALLBACK
    return _cap(
        _typescript_doc_line(source) or _typescript_surface(path, source) or _TYPESCRIPT_FALLBACK,
        max_len,
    )


def ast_describe(path: Path, *, max_len: int = 100) -> str:
    if path.suffix in {".ts", ".tsx"}:
        return _typescript_describe(path, max_len=max_len)
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
    for index in (path / "index.ts", path / "index.tsx"):
        if index.exists():
            desc = ast_describe(index, max_len=max_len)
            if desc != _TYPESCRIPT_FALLBACK:
                return desc
    children = sorted(
        {
            p.stem
            for p in path.iterdir()
            if p.is_file()
            and p.suffix in {".py", ".ts", ".tsx"}
            and not p.name.startswith("_")
            and not p.name.endswith((".d.ts", ".d.tsx"))
        }
    )
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
        language = (
            "tsx" if path.suffix == ".tsx" else "typescript" if path.suffix == ".ts" else "python"
        )
        parts.append(
            f"### {rel}\nStatic summary: {ast_descriptions.get(rel, '')}\n"
            f"```{language}\n{head}\n```\n"
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
