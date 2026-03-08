"""Handwritten source-module contracts for generation context and freshness."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jaunt.registry import SpecEntry


@dataclass(frozen=True, slots=True)
class HandwrittenSymbol:
    name: str
    kind: Literal["function", "async_function", "class", "assignment"]
    signature: str = ""
    doc_summary: str = ""
    excerpt: str = ""
    ast_dump: str = ""


@dataclass(frozen=True, slots=True)
class ModuleContract:
    source_file: str
    digest: str
    prompt_block: str
    handwritten_names: tuple[str, ...]
    symbols: tuple[HandwrittenSymbol, ...] = ()


def build_module_contract(*, entries: list[SpecEntry], expected_names: list[str]) -> ModuleContract:
    if not entries:
        empty_digest = hashlib.sha256(b"[]").hexdigest()
        return ModuleContract(
            source_file="",
            digest=empty_digest,
            prompt_block="(none)\n",
            handwritten_names=(),
            symbols=(),
        )

    source_file = entries[0].source_file
    source = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=source_file)
    generated_names = set(expected_names)

    symbols: list[HandwrittenSymbol] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name not in generated_names:
            symbols.append(
                HandwrittenSymbol(
                    name=node.name,
                    kind="function",
                    signature=_signature_line(source, node),
                    doc_summary=_first_doc_line(node),
                    ast_dump=ast.dump(node, include_attributes=False),
                )
            )
            continue

        if isinstance(node, ast.AsyncFunctionDef) and node.name not in generated_names:
            symbols.append(
                HandwrittenSymbol(
                    name=node.name,
                    kind="async_function",
                    signature=_signature_line(source, node),
                    doc_summary=_first_doc_line(node),
                    ast_dump=ast.dump(node, include_attributes=False),
                )
            )
            continue

        if isinstance(node, ast.ClassDef) and node.name not in generated_names:
            symbols.append(
                HandwrittenSymbol(
                    name=node.name,
                    kind="class",
                    signature=_class_signature(node),
                    doc_summary=_first_doc_line(node),
                    excerpt=_class_notes(node),
                    ast_dump=ast.dump(node, include_attributes=False),
                )
            )
            continue

        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for name in _assignment_names(node):
                if name in generated_names:
                    continue
                symbols.append(
                    HandwrittenSymbol(
                        name=name,
                        kind="assignment",
                        excerpt=_short_excerpt(ast.unparse(node)),
                        ast_dump=ast.dump(node, include_attributes=False),
                    )
                )

    prompt_block = _format_prompt_block(symbols)
    digest_payload = [
        {
            "name": symbol.name,
            "kind": symbol.kind,
            "signature": symbol.signature,
            "doc_summary": symbol.doc_summary,
            "excerpt": symbol.excerpt,
            "ast_dump": symbol.ast_dump,
        }
        for symbol in symbols
    ]
    raw = json.dumps(digest_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    handwritten_names = tuple(symbol.name for symbol in symbols)
    return ModuleContract(
        source_file=source_file,
        digest=digest,
        prompt_block=prompt_block,
        handwritten_names=handwritten_names,
        symbols=tuple(symbols),
    )


def test_public_api_only_by_name(entries: list[SpecEntry]) -> dict[str, bool]:
    policies: dict[str, bool] = {}
    for entry in entries:
        raw = entry.decorator_kwargs.get("public_api_only")
        if raw is None:
            policies[entry.qualname] = True
        elif isinstance(raw, bool):
            policies[entry.qualname] = raw
        else:
            policies[entry.qualname] = bool(raw)
    return policies


def _signature_line(source: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    seg = ast.get_source_segment(source, node) or ""
    first = next((line.strip() for line in seg.splitlines() if line.strip()), "")
    if first.startswith("@"):
        return f"{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}def {node.name}(...)"
    if first:
        return first.removesuffix(":")
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}(...)"


def _class_signature(node: ast.ClassDef) -> str:
    bases = ", ".join(ast.unparse(base) for base in node.bases)
    if bases:
        return f"class {node.name}({bases})"
    return f"class {node.name}"


def _class_notes(node: ast.ClassDef) -> str:
    decorators = [ast.unparse(dec) for dec in node.decorator_list]
    if not decorators:
        return ""
    return "decorators: " + ", ".join(decorators)


def _first_doc_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node, clean=True)
    if not doc:
        return ""
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _assignment_names(node: ast.Assign | ast.AnnAssign | ast.AugAssign) -> list[str]:
    if isinstance(node, ast.Assign):
        names: list[str] = []
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
        return names

    target = node.target
    if isinstance(target, ast.Name):
        return [target.id]
    return []


def _short_excerpt(text: str, *, limit: int = 140) -> str:
    flattened = " ".join((text or "").strip().split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 3].rstrip() + "..."


def _format_prompt_block(symbols: list[HandwrittenSymbol]) -> str:
    if not symbols:
        return "(none)\n"

    chunks: list[str] = []
    for symbol in symbols:
        lines = [f"kind: {symbol.kind}"]
        if symbol.signature:
            lines.append(f"signature: {symbol.signature}")
        if symbol.doc_summary:
            lines.append(f"doc: {symbol.doc_summary}")
        if symbol.excerpt:
            lines.append(f"notes: {symbol.excerpt}")
        chunks.append(f"# {symbol.name}\n" + "\n".join(lines))
    return "\n\n".join(chunks).rstrip() + "\n"
