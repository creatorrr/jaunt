"""Scoped AST mutation scoring: does the contract battery actually pin the body?"""

from __future__ import annotations

import ast
import copy
from collections.abc import Iterator

from jaunt.contract.derive import ContractBlocks, evaluate_blocks

_CMP_SWAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
}

_BINOP_SWAP: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
}

_BOOL_SWAP: dict[type[ast.boolop], type[ast.boolop]] = {
    ast.And: ast.Or,
    ast.Or: ast.And,
}


def _func_node(tree: ast.Module) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            return node
    raise ValueError("no top-level function in source")


def _mutation_targets(tree: ast.Module) -> list[ast.AST]:
    return list(ast.walk(tree))


def _skip_constant_ids(tree: ast.Module) -> set[int]:
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                skip.add(id(first.value))
    return skip


def _stmt_deletion_targets(tree: ast.Module) -> list[tuple[int, int]]:
    """Return (walk_index, stmt_index) for every deletable statement.

    Each parent that owns a ``body`` list contributes one entry per statement
    that can be removed without emptying the body. The function docstring (the
    first string-expression statement) is never deletable.
    """

    targets: list[tuple[int, int]] = []
    for walk_index, node in enumerate(ast.walk(tree)):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or len(body) <= 1:
            continue
        docstring_index = -1
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstring_index = 0
        for stmt_index, stmt in enumerate(body):
            if stmt_index == docstring_index or not isinstance(stmt, ast.stmt):
                continue
            targets.append((walk_index, stmt_index))
    return targets


def _emit_stmt_deletion(base: ast.Module, walk_index: int, stmt_index: int) -> str | None:
    clone = copy.deepcopy(base)
    parent = list(ast.walk(clone))[walk_index]
    body = getattr(parent, "body", None)
    if not isinstance(body, list) or len(body) <= 1 or stmt_index >= len(body):
        return None
    del body[stmt_index]
    if not body:
        return None
    ast.fix_missing_locations(clone)
    try:
        return ast.unparse(clone)
    except Exception:  # noqa: BLE001
        return None


def iter_mutants(func_source: str) -> Iterator[str]:
    """Yield one-mutation variants of the function source."""

    base = ast.parse(func_source)
    skip = _skip_constant_ids(base)
    nodes = _mutation_targets(base)

    for i, node in enumerate(nodes):
        for mutated in _mutate_node(base, i, node, skip):
            yield mutated

    for walk_index, stmt_index in _stmt_deletion_targets(base):
        out = _emit_stmt_deletion(base, walk_index, stmt_index)
        if out:
            yield out


def _emit(base: ast.Module, i: int, transform) -> str | None:
    clone = copy.deepcopy(base)
    target = list(ast.walk(clone))[i]
    if not transform(target):
        return None
    ast.fix_missing_locations(clone)
    try:
        return ast.unparse(clone)
    except Exception:  # noqa: BLE001
        return None


def _mutate_node(base: ast.Module, i: int, node: ast.AST, skip: set[int]) -> Iterator[str]:
    if isinstance(node, ast.Compare) and node.ops and type(node.ops[0]) in _CMP_SWAP:
        out = _emit(base, i, lambda t: _swap_cmp(t))
        if out:
            yield out
    if isinstance(node, ast.BoolOp) and type(node.op) in _BOOL_SWAP:
        out = _emit(base, i, lambda t: _swap_bool(t))
        if out:
            yield out
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOP_SWAP:
        out = _emit(base, i, lambda t: _swap_binop(t))
        if out:
            yield out
    if isinstance(node, ast.Constant) and id(node) not in skip:
        out = _emit(base, i, lambda t: _mutate_const(t))
        if out:
            yield out
    if isinstance(node, ast.Return) and node.value is not None:
        out = _emit(base, i, lambda t: _default_return(t))
        if out:
            yield out


def _swap_cmp(t: ast.AST) -> bool:
    if isinstance(t, ast.Compare) and t.ops:
        t.ops[0] = _CMP_SWAP[type(t.ops[0])]()
        return True
    return False


def _swap_bool(t: ast.AST) -> bool:
    if isinstance(t, ast.BoolOp):
        t.op = _BOOL_SWAP[type(t.op)]()
        return True
    return False


def _swap_binop(t: ast.AST) -> bool:
    if isinstance(t, ast.BinOp):
        t.op = _BINOP_SWAP[type(t.op)]()
        return True
    return False


def _mutate_const(t: ast.AST) -> bool:
    if not isinstance(t, ast.Constant):
        return False
    v = t.value
    if isinstance(v, bool):
        t.value = not v
        return True
    if isinstance(v, int):
        t.value = v + 1
        return True
    if isinstance(v, str) and v != "":
        t.value = ""
        return True
    return False


def _default_return(t: ast.AST) -> bool:
    if isinstance(t, ast.Return):
        t.value = ast.Constant(value=None)
        return True
    return False


def compute_strength(
    func_source: str,
    func_name: str,
    blocks: ContractBlocks,
    namespace: dict[str, object],
) -> tuple[int, int]:
    """Return (killed, applicable). A mutant is killed if any derived case fails."""

    if blocks.is_empty():
        # Nothing pins the body; every mutant survives by definition.
        applicable = sum(1 for _ in iter_mutants(func_source))
        return (0, applicable)

    killed = 0
    applicable = 0
    for mutant_src in iter_mutants(func_source):
        ns: dict[str, object] = dict(namespace)
        try:
            exec(compile(mutant_src, "<mutant>", "exec"), ns)  # noqa: S102
        except Exception:  # noqa: BLE001 - non-applicable mutant
            continue
        fn = ns.get(func_name)
        if not callable(fn):
            continue
        applicable += 1
        if evaluate_blocks(fn, blocks, ns):  # any failure -> killed
            killed += 1
    return (killed, applicable)


def format_strength(killed: int, applicable: int) -> str:
    return f"{killed}/{applicable}"
