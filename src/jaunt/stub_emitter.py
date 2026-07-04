"""Emit provenance-headed .pyi stubs from generated implementations."""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
from pathlib import Path

from jaunt.header import parse_stub_header

_JAUNT_DECORATORS = {"magic", "sig", "test", "contract", "preserve"}

# Bump when the stub rendering logic changes so already-emitted stubs re-emit.
_STUB_FORMAT_VERSION = "1"

_BUILTIN_NAMES = frozenset(dir(builtins))


def stub_path_for_source(source_file: str | Path) -> Path:
    return Path(source_file).with_suffix(".pyi")


def generated_content_digest(generated_source: str) -> str:
    return "sha256:" + hashlib.sha256(generated_source.encode("utf-8")).hexdigest()


def stub_inputs_digest(spec_source: str, generated_source: str) -> str:
    """Digest over every input the rendered stub derives from.

    The stub is built from the spec module's handwritten source (imports, aliases,
    plain signatures) *and* the generated implementation *and* the emitter format
    version. Comparing only the generated digest missed spec-only edits (a
    handwritten helper changing shape), so freshness keys on all three.
    """
    payload = "\x00".join((_STUB_FORMAT_VERSION, spec_source or "", generated_source or ""))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    rendered_nodes: list[ast.AST] = []
    for node in spec_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)):
            chunks.append(ast.unparse(copy.deepcopy(node)).strip())
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chosen = generated_nodes.get(node.name) if node.name in expected_names else None
            if not isinstance(chosen, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chosen = node
            clone = _function_stub_clone(chosen)
            rendered_nodes.append(clone)
            chunks.append(ast.unparse(clone).strip())
            continue

        if isinstance(node, ast.ClassDef):
            chosen = generated_nodes.get(node.name) if node.name in expected_names else None
            if not isinstance(chosen, ast.ClassDef):
                chosen = node
            clone = _class_stub_clone(chosen)
            rendered_nodes.append(clone)
            chunks.append(ast.unparse(clone).strip())

    # A stub's signatures come from the *generated* module, but its imports were
    # copied only from the spec module — so a generated-only import used in a
    # signature (e.g. `-> pd.DataFrame`) would leave `pd` undefined. Resolve any
    # such referenced-but-unprovided names from the generated module.
    prelude = _resolve_stub_references(
        rendered_nodes=rendered_nodes,
        provided=_module_bound_names(spec_tree),
        generated_tree=generated_tree,
    )
    chunks = prelude + chunks

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

    try:
        spec_source = Path(source_file).read_text(encoding="utf-8")
    except OSError:
        spec_source = ""

    # A header written before the inputs-digest field existed parses fine but lacks
    # the key; treat that as stale so it re-emits once (backward-parse-safe).
    recorded_inputs = _normalize_digest(parsed.get("inputs_digest"))
    if recorded_inputs is None:
        return "stale"
    current_inputs = _normalize_digest(stub_inputs_digest(spec_source, generated_source))
    if recorded_inputs != current_inputs:
        return "stale"
    return None


def _resolve_stub_references(
    *,
    rendered_nodes: list[ast.AST],
    provided: set[str],
    generated_tree: ast.Module,
) -> list[str]:
    referenced: set[str] = set()
    for node in rendered_nodes:
        referenced |= _referenced_load_names(node)
    missing = referenced - provided - _BUILTIN_NAMES
    if not missing:
        return []

    gen_imports = _import_bindings(generated_tree)
    gen_defs = {
        node.name: node
        for node in generated_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    gen_assigns = _assign_bindings(generated_tree)

    import_lines: list[str] = []
    supporting: list[str] = []
    any_fallbacks: list[str] = []
    for name in sorted(missing):
        if name in gen_imports:
            import_lines.append(gen_imports[name])
        elif name in gen_defs:
            node = gen_defs[name]
            if isinstance(node, ast.ClassDef):
                supporting.append(ast.unparse(_class_stub_clone(node)).strip())
            else:
                supporting.append(ast.unparse(_function_stub_clone(node)).strip())
        elif name in gen_assigns:
            supporting.append(ast.unparse(copy.deepcopy(gen_assigns[name])).strip())
        else:
            any_fallbacks.append(name)

    prelude: list[str] = []
    if import_lines:
        prelude.append("\n".join(dict.fromkeys(import_lines)))
    if any_fallbacks:
        # Never emit an unresolved name: bind it to `Any` as a last resort.
        prelude.append("from typing import Any")
        prelude.append("\n".join(f"{name} = Any" for name in any_fallbacks))
    prelude.extend(supporting)
    return prelude


def _module_bound_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name.split(".", 1)[0])
    return names


def _import_bindings(tree: ast.Module) -> dict[str, str]:
    """Map each name a top-level import binds to a single-import statement string."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                out.setdefault(bound, _single_import_stmt(node, alias))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                out.setdefault(bound, _single_import_stmt(node, alias))
    return out


def _single_import_stmt(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> str:
    if isinstance(node, ast.Import):
        return ast.unparse(ast.Import(names=[copy.deepcopy(alias)])).strip()
    stmt = ast.ImportFrom(
        module=node.module,
        names=[copy.deepcopy(alias)],
        level=int(getattr(node, "level", 0) or 0),
    )
    return ast.unparse(stmt).strip()


def _assign_bindings(tree: ast.Module) -> dict[str, ast.stmt]:
    out: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.setdefault(tgt.id, node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.setdefault(node.target.id, node)
    return out


def _referenced_load_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            names.add(child.id)
    return names


def _class_stub_clone(node: ast.ClassDef) -> ast.ClassDef:
    clone = copy.deepcopy(node)
    clone.decorator_list = [dec for dec in clone.decorator_list if not _is_jaunt_decorator(dec)]
    body: list[ast.stmt] = []
    for child in clone.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body.append(_function_stub_clone(child))
        elif isinstance(child, (ast.Assign, ast.AnnAssign)):
            body.append(child)
    clone.body = body or [ast.Expr(value=ast.Constant(value=Ellipsis))]
    ast.fix_missing_locations(clone)
    return clone


def _function_stub_clone(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.stmt:
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
