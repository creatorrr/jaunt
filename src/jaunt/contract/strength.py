"""Scoped AST mutation scoring: does the contract battery actually pin the body?"""

from __future__ import annotations

import ast
from collections.abc import Iterator

import jaunt

from jaunt.contract.cases import CaseBlocks

jaunt.magic_module(
    __name__,
    prompt=(
        "This module scores mutation-testing 'strength': how many one-off AST "
        "mutations of a function body a contract battery detects. The governed "
        "functions reuse handwritten module-level helpers that live in this same "
        "source module (jaunt.contract.strength). Reach a handwritten helper the "
        "way the other self-hosted jaunt modules do — import the source module "
        "(importlib.import_module('jaunt.contract.strength')) and read the helper "
        "off it — never reimplement it. Those handwritten helpers are: "
        "_skip_constant_ids(tree: ast.Module) -> set[int] (ids of function/class "
        "docstring Constant nodes, which must never be mutated); "
        "_mutation_targets(tree: ast.Module) -> list[ast.AST] (the ordered node "
        "list to mutate — currently list(ast.walk(tree))); _mutate_node(base: "
        "ast.Module, i: int, node: ast.AST, skip: set[int]) -> Iterator[str] "
        "(yields the unparsed source of each single-node mutation of nodes[i], "
        "skipping Constant nodes whose id is in skip); _stmt_deletion_targets(tree: "
        "ast.Module) -> list[tuple[int, int]] ((walk_index, stmt_index) pairs for "
        "every deletable statement, never a function docstring, never one that "
        "would empty a body); and _emit_stmt_deletion(base: ast.Module, "
        "walk_index: int, stmt_index: int) -> str | None (unparsed source with "
        "that statement removed, or None if the deletion is not emittable); and "
        "_evaluate_mutant_killed(pure: CaseBlocks, ns: dict[str, object]) -> bool "
        "(runs the pure derived cases against the mutant namespace under a "
        "wall-clock timeout and returns True when the mutant is killed — including "
        "when a mutation left the body non-terminating). compute_case_strength "
        "must import CaseBlocks from jaunt.contract.cases lazily inside the "
        "function body (module-level import would create an import cycle) and must "
        "decide whether each mutant is killed by calling _evaluate_mutant_killed, "
        "never by calling evaluate_cases directly (a bare evaluate_cases call would "
        "hang reconcile on a non-terminating mutant). CaseBlocks is a "
        "frozen dataclass with fields examples: tuple[CallCase, ...], raises: "
        "tuple[CallCase, ...], and fixtures_declared: tuple[str, ...]; it exposes "
        "is_empty() -> bool (True when both examples and raises are empty); each "
        "CallCase has a .fixtures: tuple[str, ...] attribute."
    ),
)

EJECT_STRENGTH_WARN = 0.5


def parse_strength(text: str) -> tuple[int, int]:
    """Parse a ``"<killed>/<applicable>"`` strength string into ``(killed, applicable)``.

    Split ``text`` on its first ``"/"``; the part before the slash is the killed
    count and the part after is the applicable count. Convert both to ``int`` and
    return them as a ``(killed, applicable)`` tuple. If either part is not a valid
    integer — including when ``text`` has no ``"/"`` (the applicable part is then
    the empty string) — return ``(0, 0)``.

    Examples:
    - ``parse_strength("2/5")`` -> ``(2, 5)``
    - ``parse_strength("0/0")`` -> ``(0, 0)``
    - ``parse_strength("bad")`` -> ``(0, 0)``
    - ``parse_strength("3/x")`` -> ``(0, 0)``
    """
    raise NotImplementedError


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


def _mutation_targets(tree: ast.Module) -> list[ast.AST]:
    return list(ast.walk(tree))


def _skip_constant_ids(tree: ast.Module) -> set[int]:
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
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
    import copy

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
    """Yield one-mutation variants of ``func_source`` as unparsed Python source.

    Parse ``func_source`` into an ``ast.Module`` (call it ``base``). Compute the
    set of docstring-constant ids to leave untouched with the handwritten helper
    ``_skip_constant_ids(base)``, and the ordered list of nodes to mutate with the
    handwritten helper ``_mutation_targets(base)``.

    Emit mutants in two phases, in this order:

    1. Node mutations, in node order: for each ``(i, node)`` from
       ``enumerate(nodes)``, yield every string produced by the handwritten helper
       ``_mutate_node(base, i, node, skip)``.
    2. Statement deletions: for each ``(walk_index, stmt_index)`` from the
       handwritten helper ``_stmt_deletion_targets(base)``, call the handwritten
       helper ``_emit_stmt_deletion(base, walk_index, stmt_index)`` and yield its
       result only when it is not ``None``.

    Every yielded value is a distinct, parseable source string differing from
    ``func_source`` by a single mutation. This is a generator; the helpers own the
    mutation and deletion semantics — do not reimplement them.
    """
    raise NotImplementedError


def _emit(base: ast.Module, i: int, transform) -> str | None:
    import copy

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


def format_strength(killed: int, applicable: int) -> str:
    """Format a strength pair as the ``"<killed>/<applicable>"`` string.

    Return ``f"{killed}/{applicable}"`` — the two integers joined by a single
    ``"/"``. This is the exact inverse of :func:`parse_strength` for well-formed
    input.

    Examples:
    - ``format_strength(2, 5)`` -> ``"2/5"``
    - ``format_strength(0, 0)`` -> ``"0/0"``
    """
    raise NotImplementedError


_MUTANT_EVAL_TIMEOUT_S: float = 2.0


def _evaluate_mutant_killed(pure: "CaseBlocks", ns: dict[str, object]) -> bool:
    """Return True when the pure derived cases KILL this mutant.

    Runs ``jaunt.contract.derive.evaluate_cases(pure, namespace=dict(ns))`` and
    reports the mutant as killed when that call surfaces at least one failure.

    Mutation targets include the exit guards of unbounded loops, so a single
    mutation can turn a terminating body into one that never returns (e.g.
    dropping ``if cur.parent == cur: break`` from a filesystem-walk). Evaluating
    such a mutant in-process with no bound would hang ``jaunt reconcile``
    indefinitely (observed: 100% CPU, no output, no model call). To defend the
    reconcile loop, when this runs on the interpreter's main thread the whole
    ``evaluate_cases`` call is bounded by a ``_MUTANT_EVAL_TIMEOUT_S``-second
    wall-clock alarm (``signal.setitimer(ITIMER_REAL, ...)``). A mutant that
    exceeds the budget diverges observably from the original (which terminates
    promptly), so a timeout counts as killed. The timeout signal is raised as a
    ``BaseException`` subclass so ``evaluate_cases``'s per-case ``except
    Exception`` cannot swallow it and let a later hanging case escape the bound.
    Off the main thread the alarm is unavailable, so evaluation runs unbounded
    (best effort) — reconcile drives strength scoring on the main thread.
    """
    import signal
    import threading

    from jaunt.contract.derive import evaluate_cases

    def _killed() -> bool:
        return bool(evaluate_cases(pure, namespace=dict(ns)))

    if threading.current_thread() is not threading.main_thread():
        return _killed()

    class _MutantTimeout(BaseException):
        pass

    def _on_alarm(_signum: int, _frame: object) -> None:
        raise _MutantTimeout()

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, _MUTANT_EVAL_TIMEOUT_S)
    try:
        return _killed()
    except _MutantTimeout:
        return True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def compute_case_strength(
    source: str,
    target: str,
    blocks: "CaseBlocks",
    namespace: dict[str, object],
) -> tuple[int, int, int]:
    """Score how many mutants of ``source`` the contract cases in ``blocks`` kill.

    Returns ``(killed, applicable, excluded)``. Fixture cases are excluded from
    scoring (mutating and re-running pytest per mutant is unbounded for DB
    fixtures); the excluded count is surfaced in the battery header.

    Import ``CaseBlocks`` from ``jaunt.contract.cases`` lazily inside this
    function (a module-level import would create an import cycle).

    Procedure:

    1. ``excluded`` is the number of cases across ``blocks.examples`` and
       ``blocks.raises`` whose ``.fixtures`` is truthy.
    2. Build ``pure``: a new ``CaseBlocks`` keeping only the cases whose
       ``.fixtures`` is falsy — ``examples`` = the non-fixture examples,
       ``raises`` = the non-fixture raises — and carrying ``blocks.fixtures_declared``
       through unchanged as ``fixtures_declared``.
    3. If ``pure.is_empty()`` (no pure cases pin the body, so every mutant
       survives): set ``applicable`` to the number of mutants produced by
       ``iter_mutants(source)`` and return ``(0, applicable, excluded)``.
    4. Otherwise, initialize ``killed = 0`` and ``applicable = 0``. For each
       ``mutant_src`` from ``iter_mutants(source)``:
       - Build ``ns = dict(namespace)`` and ``exec`` the compiled ``mutant_src``
         into ``ns``. If executing the mutant raises any exception, it is a
         non-applicable mutant — skip it (continue) without counting.
       - Look up ``ns.get(target)``; if it is not callable, skip it (continue)
         without counting.
       - The mutant is applicable: increment ``applicable``. Decide whether the
         mutant is killed with the handwritten helper
         ``_evaluate_mutant_killed(pure, ns)`` (reach it the way the other
         handwritten helpers are reached — import the source module
         ``jaunt.contract.strength`` and read the helper off it — never
         reimplement it or call ``evaluate_cases`` directly, so mutants that do
         not terminate stay bounded). If it returns ``True`` the mutant was
         detected — increment ``killed``.
    5. Return ``(killed, applicable, excluded)``.

    ``blocks`` and ``namespace`` are read at call time; the function does not read
    module-level mutable state.
    """
    raise NotImplementedError
