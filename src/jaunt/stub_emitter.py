"""Emit provenance-headed .pyi stubs from generated implementations."""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
from dataclasses import dataclass
from pathlib import Path

import jaunt
from jaunt.header import parse_stub_header

_JAUNT_DECORATORS = {"magic", "sig", "test", "contract", "preserve"}

# Bump when the stub rendering logic changes so already-emitted stubs re-emit.
_STUB_FORMAT_VERSION = "4"

_BUILTIN_NAMES = frozenset(dir(builtins))


@jaunt.contract
def stub_path_for_source(source_file: str | Path) -> Path:
    """Return the ``.pyi`` stub path that sits beside a spec ``source_file``.

    Replaces the final path suffix with ``.pyi`` via :meth:`Path.with_suffix`,
    so the stub lands next to the spec module in the same directory. The input
    may be a ``str`` or a ``Path``; the result is always a ``Path``. Only the
    final suffix is replaced — earlier dots in a name are left untouched.

    Examples:
    - stub_path_for_source("pkg/mod.py") == Path("pkg/mod.pyi")
    - stub_path_for_source(Path("a/b.py")) == Path("a/b.pyi")
    """
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


def normalize_python_source(
    source: str,
    *,
    filename: str,
    preserve_annotation_syntax: bool = False,
) -> tuple[str, list[str]]:
    """Apply Jaunt's bundled Ruff convention to generated Python source.

    Ruff is a base Jaunt dependency. Candidates are formatted, auto-fixed with
    the E/F/I/UP/B rule set (including explicitly requested unsafe fixes), then
    formatted and checked once more. The isolated configuration keeps emitted
    bytes stable across adopter repositories with different Ruff settings.
    """
    import shutil
    import subprocess

    ruff = shutil.which("ruff")
    if ruff is None:
        return source, ["Ruff normalization unavailable: the bundled ruff executable was not found"]

    format_args = [
        ruff,
        "format",
        "--isolated",
        "--line-length",
        "100",
        "--target-version",
        "py312",
        "--stdin-filename",
        filename,
        "-",
    ]
    ignored_rules = ["E501"]
    if preserve_annotation_syntax:
        # Sealed @jaunt.sig methods require the emitted annotation syntax to
        # match the authored signature exactly. These are the pyupgrade rules
        # that rewrite typing.List/Optional/Union rather than merely formatting
        # them. All other UP rules, plus E/F/I/B, remain active.
        ignored_rules.extend(["UP006", "UP007", "UP035", "UP045"])

    check_args = [
        ruff,
        "check",
        "--isolated",
        "--select",
        "E,F,I,UP,B",
        # Ruff's formatter intentionally leaves some unsplittable lines long;
        # retrying the model for formatter-owned E501 output wastes tokens.
        "--ignore",
        ",".join(ignored_rules),
        "--target-version",
        "py312",
        "--stdin-filename",
        filename,
    ]

    def run(args: list[str], text: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                args,
                input=text,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    formatted = run(format_args, source)
    if formatted is None or formatted.returncode != 0:
        detail = "" if formatted is None else (formatted.stderr or formatted.stdout).strip()
        return source, [f"Ruff format failed{': ' + detail if detail else ''}"]
    current = formatted.stdout

    fixed = run([*check_args, "--fix", "--unsafe-fixes", "-"], current)
    if fixed is None:
        return current, ["Ruff check --fix --unsafe-fixes failed to run"]
    if fixed.stdout:
        current = fixed.stdout

    reformatted = run(format_args, current)
    if reformatted is None or reformatted.returncode != 0:
        detail = "" if reformatted is None else (reformatted.stderr or reformatted.stdout).strip()
        return current, [f"Ruff format failed{': ' + detail if detail else ''}"]
    current = reformatted.stdout

    checked = run([*check_args, "--output-format", "concise", "-"], current)
    if checked is None:
        return current, ["Ruff final check failed to run"]
    if checked.returncode != 0:
        diagnostics = [line for line in checked.stdout.splitlines() if line.strip()]
        return current, diagnostics or ["Ruff final check failed"]
    return current, []


def format_stub_best_effort(stub_source: str) -> str:
    """Normalize an emitted stub with Ruff when Ruff is available.

    Projects that gate on ``ruff format --check`` have ruff by definition, so
    normalization removes the need for per-file lint exemptions on emitted
    stubs. Environments without Ruff retain the unformatted text; freshness is
    keyed on the inputs digest, never the rendered bytes.
    """
    normalized, _errors = normalize_python_source(stub_source, filename="stub.pyi")
    return normalized


@jaunt.contract
def is_jaunt_stub(path: Path) -> bool:
    """Return whether ``path`` is a jaunt-emitted ``.pyi`` stub.

    Reads ``path`` and returns ``True`` only when its text carries a parseable
    jaunt stub provenance header (via :func:`parse_stub_header`). Fail-soft: a
    missing or unreadable file returns ``False`` rather than raising, and a file
    without a recognizable header also returns ``False``.

    Examples:
    - is_jaunt_stub(Path("/nonexistent/does-not-exist.pyi")) == False
    """
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

    source_imports: list[ast.Import | ast.ImportFrom] = []
    body_chunks: list[str] = []
    rendered_nodes: list[ast.AST] = []
    for node in spec_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # __future__ imports are meaningless in stubs (forward refs are
            # implicit in .pyi) and land after the generated-import prelude,
            # where they are a syntax error — never copy them from the spec.
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            # jaunt imports exist only for the decorators/magic_module call,
            # both stripped from stubs — copying them is a guaranteed F401.
            if isinstance(node, ast.ImportFrom) and (
                node.module == "jaunt" or (node.module or "").startswith("jaunt.")
            ):
                continue
            if isinstance(node, ast.Import):
                kept = [
                    a for a in node.names if not (a.name == "jaunt" or a.name.startswith("jaunt."))
                ]
                if not kept:
                    continue
                node = ast.Import(names=kept)
            source_imports.append(copy.deepcopy(node))
            continue

        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            clone = copy.deepcopy(node)
            rendered_nodes.append(clone)
            body_chunks.append(ast.unparse(clone).strip())
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chosen = generated_nodes.get(node.name) if node.name in expected_names else None
            if not isinstance(chosen, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chosen = node
            clone = _function_stub_clone(chosen)
            rendered_nodes.append(clone)
            body_chunks.append(ast.unparse(clone).strip())
            continue

        if isinstance(node, ast.ClassDef):
            chosen = generated_nodes.get(node.name) if node.name in expected_names else None
            if not isinstance(chosen, ast.ClassDef):
                chosen = node
            clone = _class_stub_clone(chosen)
            rendered_nodes.append(clone)
            body_chunks.append(ast.unparse(clone).strip())

    # A stub's signatures come from the *generated* module, but its imports were
    # copied only from the spec module — so a generated-only import used in a
    # signature (e.g. `-> pd.DataFrame`) would leave `pd` undefined. Resolve any
    # such referenced-but-unprovided names from the generated module. Names bound
    # only by jaunt imports are NOT provided — those imports were skipped above,
    # so a legitimate reference (`-> JauntError`) must resolve like any other.
    referenced: set[str] = set()
    for node in rendered_nodes:
        referenced |= _referenced_load_names(node)
    referenced |= _explicit_stub_exports(spec_tree)
    kept_imports = [
        kept
        for node in source_imports
        if (kept := _filter_stub_import(node, referenced)) is not None
    ]
    import_chunks = [ast.unparse(node).strip() for node in kept_imports]
    all_import_names = _import_bound_names(spec_tree)
    kept_import_names = {name for node in kept_imports for name in _bound_import_names(node)}

    resolved = _resolve_stub_references(
        rendered_nodes=rendered_nodes,
        provided=(_module_bound_names(spec_tree) - all_import_names) | kept_import_names,
        generated_tree=generated_tree,
        generated_module=generated_module,
    )
    import_block = "\n".join(dict.fromkeys([*import_chunks, *resolved.imports]))
    fallback_block = "\n".join(dict.fromkeys(resolved.fallbacks))
    chunks = [import_block, fallback_block, *resolved.supporting, *body_chunks]

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


@dataclass(frozen=True)
class _ResolvedStubReferences:
    imports: list[str]
    fallbacks: list[str]
    supporting: list[str]


def _resolve_stub_references(
    *,
    rendered_nodes: list[ast.AST],
    provided: set[str],
    generated_tree: ast.Module,
    generated_module: str | None,
) -> _ResolvedStubReferences:
    referenced: set[str] = set()
    for node in rendered_nodes:
        referenced |= _referenced_load_names(node)
    queue = sorted(referenced - provided - _BUILTIN_NAMES)
    if not queue:
        return _ResolvedStubReferences(imports=[], fallbacks=[], supporting=[])

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
                # __future__ imports are illegal mid-file and meaningless in
                # stubs (forward refs are implicit in .pyi) — never emit them.
                if not gen_imports[name].startswith("from __future__"):
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

    if any_fallbacks:
        # Never emit an unresolved name: bind it to `Any` as a last resort.
        import_lines.append("from typing import Any")
    return _ResolvedStubReferences(
        imports=list(dict.fromkeys(import_lines)),
        fallbacks=[f"{name} = Any" for name in dict.fromkeys(any_fallbacks)],
        supporting=supporting,
    )


def _jaunt_import_bound_names(tree: ast.Module) -> set[str]:
    """Names bound at top level ONLY by jaunt imports (which stubs never copy)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "jaunt" or alias.name.startswith("jaunt."):
                    names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "jaunt" or module.startswith("jaunt."):
                for alias in node.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
    return names


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


def _bound_import_names(node: ast.Import | ast.ImportFrom) -> set[str]:
    names: set[str] = set()
    for alias in node.names:
        if alias.name == "*":
            continue
        names.add(alias.asname or alias.name.split(".", 1)[0])
    return names


def _import_bound_names(tree: ast.Module) -> set[str]:
    return {
        name
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for name in _bound_import_names(node)
    }


def _explicit_stub_exports(tree: ast.Module) -> set[str]:
    exports: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == "__all__"):
            continue
        value = node.value
        if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            continue
        exports.update(
            item.value
            for item in value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
    return exports


def _filter_stub_import(
    node: ast.Import | ast.ImportFrom, required_names: set[str]
) -> ast.Import | ast.ImportFrom | None:
    kept: list[ast.alias] = []
    for alias in node.names:
        if alias.name == "*":
            kept.append(copy.deepcopy(alias))
            continue
        bound = alias.asname or alias.name.split(".", 1)[0]
        explicit_reexport = alias.asname == alias.name
        if bound in required_names or explicit_reexport:
            kept.append(copy.deepcopy(alias))
    if not kept:
        return None
    if isinstance(node, ast.Import):
        return ast.Import(names=kept)
    return ast.ImportFrom(module=node.module, names=kept, level=node.level)


def _import_bindings(tree: ast.Module, *, generated_module: str | None) -> dict[str, str]:
    """Map each name a top-level import binds to a single-import statement string.

    Also descends into top-level ``if TYPE_CHECKING:`` blocks — those imports are
    checker-only by design, which is exactly the context a ``.pyi`` stub lives in,
    so they are always safe to emit plainly.
    """
    out: dict[str, str] = {}

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
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
            elif isinstance(node, ast.If) and _is_type_checking_test(node.test):
                visit(node.body)

    visit(tree.body)
    return out


def _is_type_checking_test(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


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
        elif isinstance(child, ast.arg):
            names |= _string_annotation_names(child.annotation)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names |= _string_annotation_names(child.returns)
        elif isinstance(child, ast.AnnAssign):
            names |= _string_annotation_names(child.annotation)
    return names


def _string_annotation_names(annotation: ast.expr | None) -> set[str]:
    """Names referenced inside string annotation fragments.

    A quoted annotation (``"RecursiveChunker | None"``, ``Optional["X"]``) is an
    ``ast.Constant`` — invisible to the generic ``Name`` walk — but ruff/type
    checkers still resolve the names inside it, so an unbound one is an F821 in
    the emitted stub. Parse each string fragment and surface its names so the
    resolution pass can import or ``Any``-bind them.
    """
    names: set[str] = set()
    if annotation is None:
        return names
    for child in ast.walk(annotation):
        if not (isinstance(child, ast.Constant) and isinstance(child.value, str)):
            continue
        try:
            parsed = ast.parse(child.value, mode="eval")
        except SyntaxError:
            continue
        for sub in ast.walk(parsed):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                names.add(sub.id)
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
    if isinstance(clone, ast.AsyncFunctionDef) and any(
        _is_async_contextmanager_decorator(dec) for dec in clone.decorator_list
    ):
        # A body-less async def in a .pyi is a coroutine function, not an async
        # generator, so asynccontextmanager rejects its Callable return type. Stub
        # syntax uses a plain def here to describe the decorator's generator input;
        # the decorated public callable type remains the async context manager.
        sync_clone = ast.FunctionDef(
            name=clone.name,
            args=clone.args,
            body=clone.body,
            decorator_list=clone.decorator_list,
            returns=clone.returns,
            type_comment=clone.type_comment,
            type_params=clone.type_params,
        )
        ast.copy_location(sync_clone, clone)
        clone = sync_clone
    ast.fix_missing_locations(clone)
    return clone


def _is_async_contextmanager_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id == "asynccontextmanager"
    return isinstance(target, ast.Attribute) and target.attr == "asynccontextmanager"


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
