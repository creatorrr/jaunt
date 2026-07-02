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
            method = node.attr
            node = node.value
            continue
        break
    if isinstance(node, ast.Name) and node.id == target:
        return True, method
    return False, None


def _names_in(expr: str) -> set[str]:
    return {n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)}


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
    fixtures_declared = _parse_fixtures(docstring)
    kw: _MakeCaseKw = {
        "target": target,
        "async_map": async_map,
        "fixtures_declared": fixtures_declared,
        "module_names": module_names,
        "method_override": method,
    }

    examples: list[CallCase] = []
    for line in _case_lines_for_section(docstring, "Examples"):
        pair = _split_top_level_eq(line)
        if pair is not None:
            call_expr, expected = pair
            rooted, _ = _call_root_and_method(ast.parse(call_expr, mode="eval").body, target)
            if not rooted:
                raise CaseParseError(
                    f"example call must be rooted in the target {target!r}", line=line
                )
            examples.append(
                _make_case(
                    source_line=line,
                    call_expr=call_expr,
                    expected_expr=expected,
                    exc_name=None,
                    legacy=False,
                    **kw,
                )
            )
            continue
        if "->" in line:
            left, right = line.split("->", 1)
            left, right = left.strip(), right.strip()
            if _valid_case_expr(left) and _valid_case_expr(right):
                examples.append(
                    _make_case(
                        source_line=line,
                        call_expr=f"{target}({left})",
                        expected_expr=right,
                        exc_name=None,
                        legacy=True,
                        **kw,
                    )
                )
        # Anything else under Examples: prose; skipped (legacy behavior).

    raises: list[CallCase] = []
    for line in _case_lines_for_section(docstring, "Raises"):
        if " raises " in line:
            inp, exc = line.split(" raises ", 1)
            inp, exc = inp.strip(), exc.strip().rstrip(".")
            if not (_valid_case_expr(inp) and _CASE_EXCEPTION_NAME_RE.match(exc)):
                continue
            tree = ast.parse(inp, mode="eval")
            rooted, _ = _call_root_and_method(tree.body, target)
            if rooted and isinstance(tree.body, (ast.Call, ast.Attribute)):
                raises.append(
                    _make_case(
                        source_line=line,
                        call_expr=inp,
                        expected_expr=None,
                        exc_name=exc,
                        legacy=False,
                        **kw,
                    )
                )
            else:
                raises.append(
                    _make_case(
                        source_line=line,
                        call_expr=f"{target}({inp})",
                        expected_expr=None,
                        exc_name=exc,
                        legacy=True,
                        **kw,
                    )
                )
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+on\s+(.+)$", line)
        if m and _valid_case_expr(m.group(2).strip()):
            raises.append(
                _make_case(
                    source_line=line,
                    call_expr=f"{target}({m.group(2).strip()})",
                    expected_expr=None,
                    exc_name=m.group(1),
                    legacy=True,
                    **kw,
                )
            )

    return CaseBlocks(
        examples=tuple(examples), raises=tuple(raises), fixtures_declared=fixtures_declared
    )
