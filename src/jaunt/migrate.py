"""Mechanical source migrations for Jaunt projects."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import jaunt

jaunt.magic_module(__name__)

LEGACY_STUB_MIGRATION_ID = "legacy-stub-body"
STUB_REEMIT_MIGRATION_ID = "stub-reemit"


@dataclass(frozen=True, slots=True)
class MigrationAction:
    migration_id: str
    path: Path
    module: str
    symbol: str
    kind: Literal["rewrite-stub-body", "reemit-stub"]
    classification: Literal["re-stamp", "newly-governs"]
    description: str


type FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def plan_legacy_stub_rewrites(
    *, source_file: Path, module: str, governed_symbols: set[str]
) -> list[MigrationAction]:
    """Plan the rewrite of every legacy ``raise RuntimeError("spec stub")`` body
    in one source file to a bare ``...`` ellipsis body.

    Signature is fixed: keyword-only ``source_file`` (a ``pathlib.Path``),
    ``module`` (the dotted module name the file belongs to), and
    ``governed_symbols`` (the set of qualnames Jaunt already governs in that
    module — top-level function/class names, and dotted ``Class.method`` names).

    Procedure:

    1. Read ``source_file`` as UTF-8 text and parse it with ``ast.parse``
       (passing ``filename=str(source_file)``).
    2. Enumerate the rewritable legacy stub nodes with the module-level helper
       ``_iter_rewritable_nodes(tree)``. It returns a ``list`` of
       ``(symbol, node)`` pairs, in this order: each top-level function / async
       function whose body is a legacy stub, then — for each top-level class in
       body order — each of that class's method stubs, whose ``symbol`` is the
       dotted ``ClassName.method`` qualname. Only nodes recognized as legacy
       stubs appear (a body of exactly one ``raise RuntimeError("spec stub")``
       statement, optionally preceded by a docstring; single- or double-quoted,
       but the string value must equal ``"spec stub"`` exactly — a different
       message such as ``"spec  stub"`` or a real runtime error is excluded).
    3. For each ``(symbol, node)`` pair, classify it: it is already governed when
       ``_symbol_is_governed(symbol, governed_symbols)`` is true (the qualname is
       in ``governed_symbols``, or — for a dotted ``Class.method`` — its
       enclosing class name is). Set ``classification`` to ``"re-stamp"`` when
       governed, else ``"newly-governs"``. Build a human ``label``:
       ``"re-stamp (free)"`` for a re-stamp, else ``"newly-governs"``.
    4. Append a ``MigrationAction`` for the pair, preserving the pair order, with
       ``migration_id=LEGACY_STUB_MIGRATION_ID``, ``path=source_file``,
       ``module=module``, ``symbol=symbol``, ``kind="rewrite-stub-body"``, the
       ``classification`` from step 3, and
       ``description=f"{module}.{symbol}: raise RuntimeError('spec stub') -> ... [{label}]"``.
    5. Return the list of actions (empty when the file has no legacy stubs).

    ``source_file`` is read at call time.
    """
    raise NotImplementedError


def _symbol_is_governed(symbol: str, governed_symbols: set[str]) -> bool:
    """A candidate is already governed when its own qualname is in the governed
    set, or (for a method) its enclosing class is a governed whole-class spec."""
    if symbol in governed_symbols:
        return True
    if "." in symbol:
        return symbol.split(".", 1)[0] in governed_symbols
    return False


def plan_stub_reemissions(
    *, module_specs: dict[str, list], package_dir: Path, generated_dir: str
) -> list[MigrationAction]:
    """Plan re-emission of ``.pyi`` stubs whose format/version has drifted from
    the committed generated body.

    Signature is fixed: keyword-only ``module_specs`` (a mapping of dotted module
    name -> a non-empty list of spec entry objects, each exposing a
    ``.source_file`` ``Path`` attribute), ``package_dir`` (the package root
    ``Path``), and ``generated_dir`` (the generated-directory name, e.g.
    ``"__generated__"``).

    Import ``builder`` and ``stub_emitter`` lazily from the ``jaunt`` package
    inside the function (``from jaunt import builder, stub_emitter``).

    Iterate module names in ``sorted(module_specs)`` order and, for each:

    1. Let ``entries = module_specs[module]``. Skip the module when ``entries``
       is empty/falsy.
    2. Read the committed generated source with
       ``builder._read_generated(package_dir, generated_dir, module)``. Skip the
       module when it returns ``None`` (no generated body on disk).
    3. Let ``source_file = entries[0].source_file``.
    4. Ask ``stub_emitter.stub_staleness(source_file=source_file,
       generated_source=gen_source)`` (both keyword arguments, where
       ``gen_source`` is the value from step 2) whether the stub is stale. Skip
       the module when it returns ``None`` (the stub is fresh).
    5. Otherwise append a ``MigrationAction`` with
       ``migration_id=STUB_REEMIT_MIGRATION_ID``,
       ``path=stub_emitter.stub_path_for_source(source_file)``,
       ``module=module``, ``symbol=""`` (empty — a stub re-emission targets no
       single symbol), ``kind="reemit-stub"``, ``classification="re-stamp"``, and
       ``description=f"{module}: re-emit .pyi stub (format/version drift) [re-stamp (free)]"``.

    Return the list of actions in the sorted-module order they were appended
    (empty when every stub is fresh).
    """
    raise NotImplementedError


def apply_stub_rewrite(action: MigrationAction) -> None:
    """Rewrite exactly the one legacy stub body named by ``action`` to ``...``,
    editing ``action.path`` in place and preserving the file's existing line
    endings and the target's indentation and docstring.

    Signature is fixed: a single positional ``action: MigrationAction``. Only
    ``action.path`` (the source file) and ``action.symbol`` (the target
    qualname; a dotted ``Class.method`` for a method) are consulted.

    Procedure:

    1. Open ``action.path`` for reading with ``encoding="utf-8"`` and
       ``newline=""`` (so line endings are preserved verbatim) and read the full
       text. Parse it with ``ast.parse`` (``filename=str(action.path)``).
    2. Collect target ``raise`` nodes: for every ``(symbol, node)`` from the
       module-level ``_iter_rewritable_nodes(tree)`` whose ``symbol`` equals
       ``action.symbol``, append ``_stub_raise(node)`` (that helper returns the
       node's single ``raise`` statement, skipping a leading docstring). This
       matches at most the one named symbol, never sibling methods or functions.
    3. If no targets were found, return without modifying the file.
    4. Split the source into lines with ``str.splitlines(keepends=True)``.
    5. For each collected target ``raise`` node, processed in DESCENDING
       ``lineno`` order (so an earlier edit does not shift later line numbers):
       let ``start = raise_node.lineno - 1`` and
       ``end = raise_node.end_lineno or raise_node.lineno``. Take
       ``original_line = lines[start]``; determine its line ending with the
       module-level ``_line_ending(original_line)`` helper (one of ``"\r\n"``,
       ``"\n"``, ``"\r"``, or ``""``); strip that ending off the end to get the
       line content; the indentation is the content's leading-whitespace prefix.
       Replace the slice ``lines[start:end]`` with the single line
       ``f"{indent}...{ending}"``.
    6. Write the joined lines back to ``action.path`` with ``encoding="utf-8"``
       and ``newline=""``.
    """
    raise NotImplementedError


def _iter_rewritable_nodes(tree: ast.Module) -> list[tuple[str, FunctionNode]]:
    """Yield (symbol, node) for each rewritable legacy stub.

    Methods carry their dotted `Class.method` qualname so classification and
    relocation target one method exactly, never every method in the class.
    """
    nodes: list[tuple[str, FunctionNode]] = []
    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_legacy_stub_node(top):
                nodes.append((top.name, top))
        elif isinstance(top, ast.ClassDef):
            for item in top.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    _is_legacy_stub_node(item)
                ):
                    nodes.append((f"{top.name}.{item.name}", item))
    return nodes


def _is_legacy_stub_node(node: FunctionNode) -> bool:
    body = node.body
    if len(body) == 2 and _is_docstring_expr(body[0]):
        body = body[1:]
    if len(body) != 1:
        return False

    stmt = body[0]
    if not isinstance(stmt, ast.Raise) or stmt.cause is not None:
        return False
    call = stmt.exc
    if not isinstance(call, ast.Call):
        return False
    if not isinstance(call.func, ast.Name) or call.func.id != "RuntimeError":
        return False
    if call.keywords or len(call.args) != 1:
        return False
    arg = call.args[0]
    return isinstance(arg, ast.Constant) and arg.value == "spec stub"


def _is_docstring_expr(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _stub_raise(node: FunctionNode) -> ast.Raise:
    body = node.body[1:] if len(node.body) == 2 and _is_docstring_expr(node.body[0]) else node.body
    stmt = body[0]
    if not isinstance(stmt, ast.Raise):
        raise TypeError("legacy stub node did not contain a raise statement")
    return stmt


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""
