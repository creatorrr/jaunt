"""Call-plan case grammar for contract batteries.

One grammar, three consumers (battery renderer, in-process validation,
mutation-strength scoring), so they cannot diverge. Legacy sugar
(`arg -> want`, `arg raises Exc`) is kept and marks cases `legacy=True` so the
renderer can emit byte-identical output for existing contracts.
"""

from __future__ import annotations

import ast
import builtins
import re
from dataclasses import dataclass, field
from typing import TypedDict

import jaunt

jaunt.magic_module(__name__)

_HEADER_RE = re.compile(r"([A-Za-z][A-Za-z ]*):\s*$")
_CASE_EXCEPTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FIXTURES_RE = re.compile(r"^Fixtures:\s*(.+)$", re.MULTILINE)
_BUILTIN_NAMES = frozenset(dir(builtins))


class _MakeCaseKw(TypedDict):
    target: str
    async_map: dict[str, bool]
    fixtures_declared: tuple[str, ...]
    module_names: frozenset[str]
    method_override: str | None


class CaseParseError(ValueError):
    """A case line that is explicitly call-shaped but invalid."""

    line: str

    def __init__(self, message: str, *, line: str) -> None:
        super().__init__(message)
        self.line = line


@dataclass(frozen=True, slots=True)
class CallCase:
    source_line: str
    call_expr: str
    expected_expr: str | None
    exc_name: str | None
    fixtures: tuple[str, ...]
    imports: tuple[str, ...]
    is_async: bool
    legacy: bool
    method: str | None


@dataclass(frozen=True, slots=True)
class CaseBlocks:
    examples: tuple[CallCase, ...] = ()
    raises: tuple[CallCase, ...] = ()
    fixtures_declared: tuple[str, ...] = field(default=())

    def is_empty(self) -> bool:
        return not self.examples and not self.raises

    def has_fixture_cases(self) -> bool:
        return any(c.fixtures for c in (*self.examples, *self.raises))

    def merged(self, other: CaseBlocks) -> CaseBlocks:
        seen = dict.fromkeys((*self.fixtures_declared, *other.fixtures_declared))
        return CaseBlocks(
            examples=(*self.examples, *other.examples),
            raises=(*self.raises, *other.raises),
            fixtures_declared=tuple(seen),
        )


def _case_lines_for_section(docstring: str, name: str) -> list[str]:
    lines = docstring.splitlines()
    out: list[str] = []
    collecting = False
    for raw in lines:
        line = raw.strip()
        m = _HEADER_RE.search(line)
        if m:
            collecting = m.group(1).strip().lower() == name.lower()
            continue
        if collecting:
            if not line:
                break
            if line.startswith("- "):
                out.append(line[2:].strip())
    return out


def _parse_fixtures(docstring: str) -> tuple[str, ...]:
    m = _FIXTURES_RE.search(docstring)
    if not m:
        return ()
    names = [n.strip() for n in m.group(1).split(",")]
    return tuple(n for n in names if n and _CASE_EXCEPTION_NAME_RE.match(n))


def _valid_case_expr(text: str) -> bool:
    try:
        ast.parse(text, mode="eval")
        return True
    except SyntaxError:
        return False


def _call_root_and_method(expr: ast.expr, target: str) -> tuple[bool, str | None]:
    """Return (rooted_in_target, method_name) for a call expression."""
    node = expr
    method: str | None = None
    while True:
        if isinstance(node, ast.Call):
            node = node.func
            continue
        if isinstance(node, ast.Attribute):
            if method is None:
                method = node.attr
            node = node.value
            continue
        break
    if isinstance(node, ast.Name) and node.id == target:
        return True, method
    return False, None


def _target_names(target: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _lambda_arg_names(args: ast.arguments) -> set[str]:
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    return names


def _bound_names_in_expr(tree: ast.AST) -> set[str]:
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for generator in node.generators:
                bound |= _target_names(generator.target)
        elif isinstance(node, ast.Lambda):
            bound |= _lambda_arg_names(node.args)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
    return bound


def _names_in(expr: str) -> set[str]:
    tree = ast.parse(expr, mode="eval")
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return names - _bound_names_in_expr(tree)


def _classify_names(
    *,
    source_line: str,
    exprs: list[str],
    target: str,
    fixtures_declared: tuple[str, ...],
    module_names: frozenset[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (fixtures_used, imports_needed); raise on unknown names."""
    names: set[str] = set()
    for e in exprs:
        names |= _names_in(e)
    names.discard(target)
    fixtures_used = tuple(n for n in fixtures_declared if n in names)
    names -= set(fixtures_used)
    names -= _BUILTIN_NAMES
    imports = tuple(sorted(n for n in names if n in module_names))
    unknown = sorted(names - set(imports))
    if unknown:
        raise CaseParseError(
            f"case references unknown name(s) {', '.join(unknown)!s}: not the target, "
            f"a declared fixture, a builtin, or a top-level name in the module",
            line=source_line,
        )
    return fixtures_used, imports


def _is_async_call(call_expr: str, *, target: str, async_map: dict[str, bool]) -> bool:
    tree = ast.parse(call_expr, mode="eval")
    rooted, method = _call_root_and_method(tree.body, target)
    if not rooted:
        return False
    key = f"{target}.{method}" if method else target
    return bool(async_map.get(key, False))


def _make_case(
    *,
    source_line: str,
    call_expr: str,
    expected_expr: str | None,
    exc_name: str | None,
    legacy: bool,
    target: str,
    async_map: dict[str, bool],
    fixtures_declared: tuple[str, ...],
    module_names: frozenset[str],
    method_override: str | None,
) -> CallCase:
    exprs = [call_expr] + ([expected_expr] if expected_expr else [])
    if exc_name is not None and exc_name not in _BUILTIN_NAMES:
        exprs.append(exc_name)
    fixtures_used, imports = _classify_names(
        source_line=source_line,
        exprs=exprs,
        target=target,
        fixtures_declared=fixtures_declared,
        module_names=module_names,
    )
    tree = ast.parse(call_expr, mode="eval")
    _, method = _call_root_and_method(tree.body, target)
    return CallCase(
        source_line=source_line,
        call_expr=call_expr,
        expected_expr=expected_expr,
        exc_name=exc_name,
        fixtures=fixtures_used,
        imports=imports,
        is_async=_is_async_call(call_expr, target=target, async_map=async_map),
        legacy=legacy,
        method=method_override if method_override is not None else method,
    )


def _split_top_level_eq(line: str) -> tuple[str, str] | None:
    """Split `call == expected` on a top-level `==` (not one inside args)."""
    if "==" not in line:
        return None
    try:
        tree = ast.parse(line, mode="eval")
    except SyntaxError:
        return None
    node = tree.body
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
    ):
        return ast.get_source_segment(line, node.left) or "", (
            ast.get_source_segment(line, node.comparators[0]) or ""
        )
    return None


def parse_case_blocks(
    docstring: str,
    *,
    target: str,
    async_map: dict[str, bool],
    module_names: frozenset[str],
    method: str | None = None,
) -> CaseBlocks:
    r"""Parse a spec ``docstring`` into a :class:`CaseBlocks` of executable call
    cases, reading the ``Examples:`` and ``Raises:`` sections.

    Keyword-only parameters: ``target`` is the symbol name each case is rooted
    in (a function or class name); ``async_map`` maps ``target`` and
    ``"{target}.{method}"`` keys to booleans marking async calls; ``module_names``
    is the frozenset of top-level names in the target's module (used to classify
    a referenced name as an import); ``method`` is an optional method-override
    name applied to every case built here (default ``None``).

    Both sections are extracted line-by-line with the module helper
    ``_case_lines_for_section(docstring, "Examples")`` and
    ``_case_lines_for_section(docstring, "Raises")`` (each returns the stripped
    text of the ``- ``-prefixed bullet lines under that header). Declared
    fixtures come from ``_parse_fixtures(docstring)`` and are stored on the
    result as ``fixtures_declared``.

    Every case is built by calling the module helper ``_make_case(...)`` with
    keyword arguments ``source_line`` (the raw bullet line), ``call_expr``,
    ``expected_expr``, ``exc_name``, ``legacy``, plus the shared keywords
    ``target=target``, ``async_map=async_map``,
    ``fixtures_declared=fixtures_declared``, ``module_names=module_names``, and
    ``method_override=method``. ``_make_case`` performs name classification and
    may raise ``CaseParseError``; do not catch it. Preserve the order in which
    cases are appended (source order within each section).

    Examples section — for each bullet line, in order:

    1. Call-equality form: try ``_split_top_level_eq(line)``. When it returns a
       ``(call_expr, expected)`` pair, the line is an explicit ``call == expected``
       case. Verify the call is rooted in the target with
       ``_call_root_and_method(ast.parse(call_expr, mode="eval").body, target)``;
       if the returned ``rooted`` flag is false, raise
       ``CaseParseError(f"example call must be rooted in the target {target!r}", line=line)``.
       Otherwise append a case built with ``call_expr=call_expr``,
       ``expected_expr=expected``, ``exc_name=None``, ``legacy=False``. Then move
       to the next line.
    2. Legacy arrow form: otherwise, if ``"->"`` is in the line, split on the
       first ``"->"`` into ``left``/``right`` and strip both. Only when
       ``_valid_case_expr(left)`` and ``_valid_case_expr(right)`` are both true,
       append a case with ``call_expr=f"{target}({left})"``,
       ``expected_expr=right``, ``exc_name=None``, ``legacy=True``. (Invalid
       arrow lines are silently skipped.)
    3. Anything else is prose and is skipped (no case, no error).

    Raises section — for each bullet line, in order:

    1. ``" raises "`` form: if the literal substring ``" raises "`` is present,
       split on its first occurrence into ``inp`` and ``exc``; strip ``inp`` and
       strip ``exc`` then strip a single trailing ``"."`` from it
       (``exc.strip().rstrip(".")``). Skip the line (no case) unless both
       ``_valid_case_expr(inp)`` is true and ``_CASE_EXCEPTION_NAME_RE.match(exc)``
       matches. Then parse ``inp`` (``ast.parse(inp, mode="eval")``) and compute
       ``rooted`` via ``_call_root_and_method(tree.body, target)``. If ``rooted``
       is true AND ``tree.body`` is an ``ast.Call`` or ``ast.Attribute``, append
       a call-form case with ``call_expr=inp``, ``expected_expr=None``,
       ``exc_name=exc``, ``legacy=False``. Otherwise append a legacy case with
       ``call_expr=f"{target}({inp})"``, ``expected_expr=None``,
       ``exc_name=exc``, ``legacy=True``. Then move to the next line.
    2. Legacy ``"<Exc> on <input>"`` form: otherwise, match the line against
       ``re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+on\s+(.+)$", line)``. If it
       matches and ``_valid_case_expr`` of the stripped second group is true,
       append a legacy case with ``call_expr=f"{target}({<group2 stripped>})"``,
       ``expected_expr=None``, ``exc_name=<group1>``, ``legacy=True``.
       (Non-matching lines are skipped.)

    Return ``CaseBlocks(examples=tuple(examples), raises=tuple(raises),
    fixtures_declared=fixtures_declared)``.

    Examples:
        - An ``Examples:`` bullet ``f(1, 2) == 3`` (target ``f``) yields one
          non-legacy example whose ``call_expr`` is ``"f(1, 2)"``.
        - An ``Examples:`` bullet ``'a b' -> 'a-b'`` yields a ``legacy=True``
          example with ``call_expr == "f('a b')"`` and ``expected_expr == "'a-b'"``.
        - A ``Raises:`` bullet ``'' raises ValueError`` yields a legacy raises
          case with ``exc_name == "ValueError"``.

    Raises:
        - An ``Examples:`` bullet whose call is not rooted in ``target`` (e.g.
          ``g(1) == 2`` when ``target`` is ``"f"``) raises ``CaseParseError``.
    """
    raise NotImplementedError
