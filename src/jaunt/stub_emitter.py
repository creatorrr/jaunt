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

    Both sources are AST-normalized before hashing so comment- and formatting-only
    edits do not restale the stub — preserving the Layer-A ``jaunt status`` guarantee
    that ruff/whitespace/comment churn is not drift.
    """
    payload = "\x00".join(
        (
            _STUB_FORMAT_VERSION,
            _normalize_source_for_digest(spec_source),
            _normalize_source_for_digest(generated_source),
        )
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_source_for_digest(source: str) -> str:
    """Canonicalize Python source (strip comments/formatting) via parse+unparse.

    Falls back to the raw text if the source does not parse, so an unexpected input
    still produces a deterministic digest.
    """
    try:
        return ast.unparse(ast.parse(source or ""))
    except SyntaxError:
        return source or ""


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
    generated_module: str | None = None,
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
        generated_module=generated_module,
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


# Safety cap on the resolution fixpoint; deep transitive chains are exotic and a
# bounded loop guards against a pathological cycle in generated definitions.
_MAX_RESOLVE_ITERATIONS = 10


def _resolve_stub_references(
    *,
    rendered_nodes: list[ast.AST],
    provided: set[str],
    generated_tree: ast.Module,
    generated_module: str | None,
) -> list[str]:
    referenced: set[str] = set()
    for node in rendered_nodes:
        referenced |= _referenced_load_names(node)
    queue = sorted(referenced - provided - _BUILTIN_NAMES)
    if not queue:
        return []

    gen_imports = _import_bindings(generated_tree, generated_module=generated_module)
    gen_defs = {
        node.name: node
        for node in generated_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    gen_assigns = _assign_bindings(generated_tree)

    # Resolve to a fixpoint: a supporting definition may itself reference further
    # generated-only names (e.g. `make() -> _Result` where `_Result.inner: _Inner`),
    # so pulling one in can surface new missing names to resolve on the next pass.
    resolved = set(provided) | _BUILTIN_NAMES
    import_lines: list[str] = []
    supporting: list[str] = []
    any_fallbacks: list[str] = []
    for _ in range(_MAX_RESOLVE_ITERATIONS):
        if not queue:
            break
        next_queue: set[str] = set()
        for name in queue:
            if name in resolved:
                continue
            resolved.add(name)
            if name in gen_imports:
                import_lines.append(gen_imports[name])
            elif name in gen_defs:
                node = gen_defs[name]
                clone = (
                    _class_stub_clone(node)
                    if isinstance(node, ast.ClassDef)
                    else _function_stub_clone(node)
                )
                supporting.append(ast.unparse(clone).strip())
                next_queue |= _referenced_load_names(clone)
            elif name in gen_assigns:
                node = gen_assigns[name]
                supporting.append(ast.unparse(copy.deepcopy(node)).strip())
                next_queue |= _referenced_load_names(node)
            else:
                any_fallbacks.append(name)
        queue = sorted(next_queue - resolved)

    prelude: list[str] = []
    if import_lines:
        prelude.append("\n".join(dict.fromkeys(import_lines)))
    if any_fallbacks:
        # Never emit an unresolved name: bind it to `Any` as a last resort.
        prelude.append("from typing import Any")
        prelude.append("\n".join(f"{name} = Any" for name in dict.fromkeys(any_fallbacks)))
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


def _import_bindings(tree: ast.Module, *, generated_module: str | None) -> dict[str, str]:
    """Map each name a top-level import binds to a single-import statement string."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                out.setdefault(bound, _single_import_stmt(node, alias, generated_module))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                out.setdefault(bound, _single_import_stmt(node, alias, generated_module))
    return out


def _single_import_stmt(
    node: ast.Import | ast.ImportFrom, alias: ast.alias, generated_module: str | None
) -> str:
    if isinstance(node, ast.Import):
        return ast.unparse(ast.Import(names=[copy.deepcopy(alias)])).strip()
    level = int(getattr(node, "level", 0) or 0)
    module = node.module
    # The stub lives at the spec module's location, not the generated module's, so a
    # relative import copied verbatim would re-resolve to the wrong package. Rewrite
    # it to the absolute module it targets from inside the generated module.
    if level > 0:
        absolute = _resolve_relative_generated_module(generated_module, level, module)
        if absolute is not None:
            module, level = absolute, 0
    stmt = ast.ImportFrom(module=module, names=[copy.deepcopy(alias)], level=level)
    return ast.unparse(stmt).strip()


def _resolve_relative_generated_module(
    generated_module: str | None, level: int, module: str | None
) -> str | None:
    """Resolve a relative ``from`` import inside the generated module to an absolute name."""
    if not generated_module or level <= 0:
        return None
    parts = generated_module.split(".")
    anchor = parts[: len(parts) - level]
    if not anchor:
        return None
    base = ".".join(anchor)
    return f"{base}.{module}" if module else base


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
