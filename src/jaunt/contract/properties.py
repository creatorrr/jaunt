"""Property/invariant cases for contract batteries (Hypothesis-backed).

Tier-1 grammar (deterministic, no model), one bullet per property under a
``Properties:`` docstring section::

    Properties:
    - given t: str :: slugify(slugify(t)) == slugify(t)
    - given xs: st.lists(st.integers()) :: sorted(dedupe(xs)) == sorted(set(xs))

``given <bindings> :: <boolean-expr>`` — each binding is ``name: <type>`` (maps
to ``st.from_type(<type>)``) or ``name: st.<...>`` (an explicit strategy, passed
through verbatim). Bullets that are not ``given``-shaped are collected as prose
for the model tier at reconcile. A bullet that IS ``given``-shaped but invalid
raises :class:`~jaunt.contract.cases.CaseParseError`, mirroring how explicitly
call-shaped example bullets fail loudly.

Handwritten by choice, like ``contract/derive.py``: rendered battery bytes feed
the deterministic ``check`` gate. Kept outside ``contract/cases.py`` so the
self-hosted magic spec there is untouched.

v1 limits (each rejected with an actionable error rather than emitting an
unsound battery): no pytest fixtures in property bullets (Hypothesis reuses a
function-scoped fixture across every generated example and flags it with
``HealthCheck.function_scoped_fixture``), and no async targets (an ``await``
cannot appear in an ``eval``-mode invariant expression).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from jaunt.contract.battery import DerivedRegion


def _cases_helpers():
    """The shared case grammar helpers (handwritten context in contract/cases.py)."""
    import jaunt.contract.cases as cases

    return cases


@dataclass(frozen=True, slots=True)
class PropertyBinding:
    name: str
    strategy_expr: str


@dataclass(frozen=True, slots=True)
class PropertyCase:
    source_line: str
    bindings: tuple[PropertyBinding, ...]
    expr: str
    imports: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PropertyBlocks:
    cases: tuple[PropertyCase, ...] = ()
    prose: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.cases and not self.prose

    def merged(self, other: PropertyBlocks) -> PropertyBlocks:
        return PropertyBlocks(
            cases=(*self.cases, *other.cases),
            prose=(*self.prose, *other.prose),
        )


_GIVEN_PREFIX = "given "
_SEPARATOR = " :: "


def _is_st_rooted(node: ast.expr) -> bool:
    while True:
        if isinstance(node, ast.Call):
            node = node.func
            continue
        if isinstance(node, ast.Attribute):
            node = node.value
            continue
        break
    return isinstance(node, ast.Name) and node.id == "st"


def _parse_bindings(head: str, *, line: str) -> tuple[PropertyBinding, ...]:
    cases = _cases_helpers()
    src = "{" + head + "}"
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError:
        raise cases.CaseParseError(
            "property bindings must be 'name: type-or-strategy' pairs", line=line
        ) from None
    node = tree.body
    if not isinstance(node, ast.Dict) or not node.keys:
        raise cases.CaseParseError(
            "property bindings must be 'name: type-or-strategy' pairs", line=line
        )
    bindings: list[PropertyBinding] = []
    seen: set[str] = set()
    for key, value in zip(node.keys, node.values, strict=True):
        if not isinstance(key, ast.Name):
            raise cases.CaseParseError(
                "property binding names must be plain identifiers", line=line
            )
        if key.id in seen:
            raise cases.CaseParseError(f"duplicate property binding name {key.id!r}", line=line)
        seen.add(key.id)
        value_src = (ast.get_source_segment(src, value) or "").strip()
        if not value_src:
            raise cases.CaseParseError(
                f"property binding {key.id!r} has no type or strategy", line=line
            )
        strategy = value_src if _is_st_rooted(value) else f"st.from_type({value_src})"
        bindings.append(PropertyBinding(name=key.id, strategy_expr=strategy))
    return tuple(bindings)


def _rooted_calls(tree: ast.AST, target: str) -> list[str | None]:
    """Method names (None for a bare call) of every call rooted in ``target``."""
    cases = _cases_helpers()
    methods: list[str | None] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            rooted, method = cases._call_root_and_method(node, target)
            if rooted:
                methods.append(method)
    return methods


def _classify_property_names(
    *,
    line: str,
    expr_srcs: list[str],
    binding_names: set[str],
    target: str,
    fixtures_declared: tuple[str, ...],
    module_names: frozenset[str],
) -> tuple[str, ...]:
    cases = _cases_helpers()
    names: set[str] = set()
    for src in expr_srcs:
        names |= cases._names_in(src)
    names -= binding_names
    names.discard(target)
    names.discard("st")
    names -= cases._BUILTIN_NAMES
    fixture_hits = sorted(n for n in names if n in fixtures_declared)
    if fixture_hits:
        raise cases.CaseParseError(
            f"property cases cannot use fixtures ({', '.join(fixture_hits)}): Hypothesis "
            "reuses a function-scoped fixture across every generated example. Inline the "
            "setup in the invariant expression or keep the case in Examples:",
            line=line,
        )
    imports = tuple(sorted(n for n in names if n in module_names))
    unknown = sorted(names - set(imports))
    if unknown:
        raise cases.CaseParseError(
            f"property references unknown name(s) {', '.join(unknown)!s}: not the target, "
            f"a binding, a builtin, 'st', or a top-level name in the module",
            line=line,
        )
    return imports


def parse_property_blocks(
    docstring: str,
    *,
    target: str,
    async_map: dict[str, bool],
    module_names: frozenset[str],
) -> PropertyBlocks:
    """Parse the ``Properties:`` section of ``docstring`` into Tier-1 cases plus
    leftover prose bullets (candidates for the model tier at reconcile)."""
    cases = _cases_helpers()
    lines = cases._case_lines_for_section(docstring, "Properties")
    if not lines:
        return PropertyBlocks()
    fixtures_declared = cases._parse_fixtures(docstring)

    parsed: list[PropertyCase] = []
    prose: list[str] = []
    for line in lines:
        if not line.startswith(_GIVEN_PREFIX) or _SEPARATOR not in line:
            prose.append(line)
            continue
        head, _, expr = line[len(_GIVEN_PREFIX) :].partition(_SEPARATOR)
        head, expr = head.strip(), expr.strip()
        bindings = _parse_bindings(head, line=line)
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError:
            raise cases.CaseParseError(
                "property invariant must be a valid Python expression", line=line
            ) from None
        rooted_methods = _rooted_calls(tree, target)
        if not rooted_methods:
            raise cases.CaseParseError(
                f"property invariant must call the target {target!r}", line=line
            )
        for method in rooted_methods:
            key = f"{target}.{method}" if method else target
            if async_map.get(key, False):
                raise cases.CaseParseError(
                    "async targets are not supported in Properties bullets (v1)", line=line
                )
        binding_names = {b.name for b in bindings}
        imports = _classify_property_names(
            line=line,
            expr_srcs=[expr, *(b.strategy_expr for b in bindings)],
            binding_names=binding_names,
            target=target,
            fixtures_declared=fixtures_declared,
            module_names=module_names,
        )
        parsed.append(PropertyCase(source_line=line, bindings=bindings, expr=expr, imports=imports))
    return PropertyBlocks(cases=tuple(parsed), prose=tuple(prose))


def properties_extra_imports(blocks: PropertyBlocks) -> tuple[str, ...]:
    names: set[str] = set()
    for case in blocks.cases:
        names.update(case.imports)
    return tuple(sorted(names))


_HYPOTHESIS_IMPORTS = (
    "from hypothesis import given, settings",
    "from hypothesis import strategies as st",
)


def render_properties_region(
    cases: tuple[PropertyCase, ...],
    *,
    max_examples: int,
    region_suffix: str = "",
) -> DerivedRegion:
    region_id = f"properties-{region_suffix}" if region_suffix else "properties"
    fn_prefix = f"test_prop_{region_suffix}" if region_suffix else "test_prop"
    settings_line = (
        f"@settings(max_examples={max_examples}, derandomize=True, database=None, deadline=None)"
    )
    blocks: list[str] = []
    for i, case in enumerate(cases, start=1):
        given_args = ", ".join(f"{b.name}={b.strategy_expr}" for b in case.bindings)
        params = ", ".join(b.name for b in case.bindings)
        blocks.append(
            "\n".join(
                [
                    f"@given({given_args})",
                    settings_line,
                    f"def {fn_prefix}_{i}({params}):  # derived from: Properties",
                    f"    assert {case.expr}",
                ]
            )
        )
    code = "\n".join(_HYPOTHESIS_IMPORTS) + "\n\n" + "\n\n".join(blocks)
    return DerivedRegion(region_id=region_id, code=code)
