"""Emit provenance-headed .pyi stubs from generated implementations."""

from __future__ import annotations

import ast
import copy
import hashlib
from pathlib import Path

from jaunt.header import parse_stub_header

_JAUNT_DECORATORS = {"magic", "sig", "test", "contract", "preserve"}


def stub_path_for_source(source_file: str | Path) -> Path:
    return Path(source_file).with_suffix(".pyi")


def generated_content_digest(generated_source: str) -> str:
    return "sha256:" + hashlib.sha256(generated_source.encode("utf-8")).hexdigest()


def is_jaunt_stub(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return parse_stub_header(text) is not None


def build_stub_source(
    spec_source: str,
    generated_source: str,
    expected_names: set[str],
    header: str,
) -> str:
    spec_tree = ast.parse(spec_source)
    generated_tree = ast.parse(generated_source or "")
    generated_nodes = {
        node.name: node
        for node in generated_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }

    chunks: list[str] = []
    for node in spec_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)):
            chunks.append(ast.unparse(copy.deepcopy(node)).strip())
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chosen = generated_nodes.get(node.name) if node.name in expected_names else None
            if not isinstance(chosen, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chosen = node
            chunks.append(_render_function_stub(chosen))
            continue

        if isinstance(node, ast.ClassDef):
            chosen = generated_nodes.get(node.name) if node.name in expected_names else None
            if not isinstance(chosen, ast.ClassDef):
                chosen = node
            chunks.append(_render_class_stub(chosen))

    body = "\n\n\n".join(chunk for chunk in chunks if chunk).rstrip()
    if not body:
        return header + "\n"
    return header + "\n" + body + "\n"


def stub_staleness(*, source_file: str | Path, generated_source: str) -> str | None:
    stub_path = stub_path_for_source(source_file)
    if not stub_path.exists():
        return "missing"
    try:
        text = stub_path.read_text(encoding="utf-8")
    except OSError:
        return "missing"
    parsed = parse_stub_header(text)
    if parsed is None:
        return None
    recorded = _normalize_digest(parsed.get("generated_digest"))
    current = _normalize_digest(generated_content_digest(generated_source))
    if recorded != current:
        return "stale"
    return None


def _render_function_stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    clone = copy.deepcopy(node)
    clone.decorator_list = [dec for dec in clone.decorator_list if not _is_jaunt_decorator(dec)]
    clone.body = [ast.Expr(value=ast.Constant(value=Ellipsis))]
    ast.fix_missing_locations(clone)
    return ast.unparse(clone).strip()


def _render_class_stub(node: ast.ClassDef) -> str:
    clone = copy.deepcopy(node)
    clone.decorator_list = [dec for dec in clone.decorator_list if not _is_jaunt_decorator(dec)]
    body: list[ast.stmt] = []
    for child in clone.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body.append(_function_stub_node(child))
        elif isinstance(child, (ast.Assign, ast.AnnAssign)):
            body.append(child)
    clone.body = body or [ast.Expr(value=ast.Constant(value=Ellipsis))]
    ast.fix_missing_locations(clone)
    return ast.unparse(clone).strip()


def _function_stub_node(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.stmt:
    clone = copy.deepcopy(node)
    clone.decorator_list = [dec for dec in clone.decorator_list if not _is_jaunt_decorator(dec)]
    clone.body = [ast.Expr(value=ast.Constant(value=Ellipsis))]
    ast.fix_missing_locations(clone)
    return clone


def _is_jaunt_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id in _JAUNT_DECORATORS
    return (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "jaunt"
        and target.attr in _JAUNT_DECORATORS
    )


def _normalize_digest(digest: str | None) -> str | None:
    if not digest:
        return None
    return digest.split(":", 1)[1] if digest.startswith("sha256:") else digest
