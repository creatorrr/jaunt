"""Mechanical source migrations for Jaunt projects."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    source = source_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_file))

    actions: list[MigrationAction] = []
    for symbol, display_name, _node in _iter_rewritable_nodes(tree):
        classification: Literal["re-stamp", "newly-governs"]
        classification = "re-stamp" if symbol in governed_symbols else "newly-governs"
        label = "re-stamp (free)" if classification == "re-stamp" else "newly-governs"
        actions.append(
            MigrationAction(
                migration_id=LEGACY_STUB_MIGRATION_ID,
                path=source_file,
                module=module,
                symbol=symbol,
                kind="rewrite-stub-body",
                classification=classification,
                description=(
                    f"{module}.{display_name}: raise RuntimeError('spec stub') -> ... [{label}]"
                ),
            )
        )
    return actions


def plan_stub_reemissions(
    *, module_specs: dict[str, list], package_dir: Path, generated_dir: str
) -> list[MigrationAction]:
    from jaunt import builder, stub_emitter

    actions: list[MigrationAction] = []
    for module in sorted(module_specs):
        entries = module_specs[module]
        if not entries:
            continue
        gen_source = builder._read_generated(package_dir, generated_dir, module)
        if gen_source is None:
            continue
        source_file = entries[0].source_file
        if (
            stub_emitter.stub_staleness(source_file=source_file, generated_source=gen_source)
            is None
        ):
            continue
        actions.append(
            MigrationAction(
                migration_id=STUB_REEMIT_MIGRATION_ID,
                path=stub_emitter.stub_path_for_source(source_file),
                module=module,
                symbol="",
                kind="reemit-stub",
                classification="re-stamp",
                description=f"{module}: re-emit .pyi stub (format/version drift) [re-stamp (free)]",
            )
        )
    return actions


def apply_stub_rewrite(action: MigrationAction) -> None:
    with action.path.open(encoding="utf-8", newline="") as f:
        source = f.read()
    tree = ast.parse(source, filename=str(action.path))

    targets: list[ast.Raise] = []
    for symbol, _display_name, node in _iter_rewritable_nodes(tree):
        if symbol == action.symbol:
            targets.append(_stub_raise(node))

    if not targets:
        return

    lines = source.splitlines(keepends=True)
    for raise_node in sorted(targets, key=lambda node: node.lineno, reverse=True):
        start = raise_node.lineno - 1
        end = raise_node.end_lineno or raise_node.lineno
        original_line = lines[start]
        ending = _line_ending(original_line)
        content = original_line[: -len(ending)] if ending else original_line
        indent = content[: len(content) - len(content.lstrip())]
        lines[start:end] = [f"{indent}...{ending}"]

    with action.path.open("w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))


def _iter_rewritable_nodes(tree: ast.Module) -> list[tuple[str, str, FunctionNode]]:
    nodes: list[tuple[str, str, FunctionNode]] = []
    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_legacy_stub_node(top):
                nodes.append((top.name, top.name, top))
        elif isinstance(top, ast.ClassDef):
            for item in top.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    _is_legacy_stub_node(item)
                ):
                    nodes.append((top.name, f"{top.name}.{item.name}", item))
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
