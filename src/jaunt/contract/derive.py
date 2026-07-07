"""Derive a structured contract from docstring prose and render it to pytest.

v1 is deterministic for structured `Examples:`/`Raises:` blocks (single positional
argument). The model fallback for unstructured prose is added in a later task.
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jaunt.contract.battery import DerivedRegion
from jaunt.generate.shared import strip_markdown_fences


@dataclass(frozen=True, slots=True)
class ExampleRow:
    input_expr: str
    expected_expr: str


@dataclass(frozen=True, slots=True)
class RaisesRow:
    input_expr: str
    exc_name: str


@dataclass(frozen=True, slots=True)
class PropertyRow:
    """A model-derived property in Tier-1 form: bindings like ``"t: str"`` plus a
    boolean invariant expression. Rendered back into a ``given … :: …`` bullet and
    re-parsed by the deterministic grammar, so a malformed row fails loudly."""

    bindings: str
    expr: str


@dataclass(frozen=True, slots=True)
class ContractBlocks:
    examples: tuple[ExampleRow, ...] = ()
    raises: tuple[RaisesRow, ...] = ()
    properties: tuple[PropertyRow, ...] = ()

    def is_empty(self) -> bool:
        return not self.examples and not self.raises and not self.properties


async def extract_blocks_via_model(
    prose: str,
    *,
    complete: Callable[[str, str], Awaitable[str]],
    func_name: str = "f",
) -> ContractBlocks:
    from jaunt.generate.shared import load_prompt, render_template

    system = load_prompt("contract_derive_system.md", None)
    user = render_template(
        load_prompt("contract_derive_user.md", None),
        {"func_name": func_name, "prose": prose},
    )
    raw = await complete(system, user)
    payload = json.loads(strip_markdown_fences(raw))

    examples = tuple(
        ExampleRow(str(row["input"]), str(row["expected"]))
        for row in payload.get("examples", [])
        if "input" in row and "expected" in row
    )
    raises = tuple(
        RaisesRow(str(row["input"]), str(row["exc"]))
        for row in payload.get("raises", [])
        if "input" in row and "exc" in row
    )
    properties = tuple(
        PropertyRow(str(row["bindings"]), str(row["expr"]))
        for row in payload.get("properties", [])
        if "bindings" in row and "expr" in row
    )
    return ContractBlocks(examples=examples, raises=raises, properties=properties)


def _render_examples_region(rows: tuple[ExampleRow, ...], func_name: str) -> DerivedRegion:
    cases = ",\n        ".join(f"({r.input_expr}, {r.expected_expr})" for r in rows)
    code = (
        f'@pytest.mark.parametrize("arg,want", [\n        {cases},\n    ])\n'
        f"def test_examples(arg, want):  # derived from: Examples\n"
        f"    assert {func_name}(arg) == want"
    )
    return DerivedRegion(region_id="examples", code=code)


def _render_errors_region(rows: tuple[RaisesRow, ...], func_name: str) -> DerivedRegion:
    by_exc: dict[str, list[str]] = {}
    for r in rows:
        by_exc.setdefault(r.exc_name, []).append(r.input_expr)
    blocks: list[str] = []
    for exc, inputs in by_exc.items():
        params = ", ".join(inputs)
        fn_suffix = exc.lower()
        blocks.append(
            f'@pytest.mark.parametrize("arg", [{params}])\n'
            f"def test_raises_{fn_suffix}(arg):  # derived from: Raises\n"
            f"    with pytest.raises({exc}):\n"
            f"        {func_name}(arg)"
        )
    return DerivedRegion(region_id="errors", code="\n\n".join(blocks))


def _resolve_exc(name: str, namespace: dict[str, object]) -> type[BaseException]:
    obj = namespace.get(name, getattr(builtins, name, None))
    if isinstance(obj, type) and issubclass(obj, BaseException):
        return obj
    raise ValueError(f"Unknown exception type in contract: {name!r}")


# ---------------------------------------------------------------------------
# Call-plan IR renderers (adoption parity). The legacy renderers above are kept
# so all-legacy blocks emit byte-identical output.
# ---------------------------------------------------------------------------

from jaunt.contract.cases import CallCase, CaseBlocks  # noqa: E402


def battery_extra_imports(blocks: CaseBlocks) -> tuple[str, ...]:
    names: set[str] = set()
    for case in (*blocks.examples, *blocks.raises):
        names.update(case.imports)
    return tuple(sorted(names))


def _fixture_params(cases: tuple[CallCase, ...], declared: tuple[str, ...]) -> str:
    used = {f for c in cases for f in c.fixtures}
    ordered = [f for f in declared if f in used]
    return ", ".join(ordered)


def _all_plain_legacy(cases: tuple[CallCase, ...]) -> bool:
    return all(c.legacy and not c.is_async and not c.fixtures for c in cases)


def _suffix_id(base: str, region_suffix: str) -> str:
    return f"{base}-{region_suffix}" if region_suffix else base


def _suffix_fn(base: str, region_suffix: str) -> str:
    return f"{base}_{region_suffix}" if region_suffix else base


def _render_examples_cases(
    cases: tuple[CallCase, ...],
    *,
    declared: tuple[str, ...],
    region_suffix: str,
) -> DerivedRegion:
    if _all_plain_legacy(cases) and not region_suffix:
        # Legacy cases are parser-built as target(<input>), so slicing between
        # the first "(" and trailing ")" recovers the exact original input
        # expression before the old renderer produces today's bytes.
        rows = tuple(
            ExampleRow(c.call_expr[c.call_expr.index("(") + 1 : -1], c.expected_expr or "")
            for c in cases
        )
        target = cases[0].call_expr[: cases[0].call_expr.index("(")]
        return _render_examples_region(rows, target)
    is_async = any(c.is_async for c in cases)
    params = _fixture_params(cases, declared)
    prefix = "async def" if is_async else "def"
    fn_name = _suffix_fn("test_examples", region_suffix)
    lines = []
    if is_async:
        lines.append("@pytest.mark.asyncio")
    lines.append(f"{prefix} {fn_name}({params}):  # derived from: Examples")
    for c in cases:
        call = f"await {c.call_expr}" if c.is_async else c.call_expr
        lines.append(f"    assert {call} == {c.expected_expr}")
    return DerivedRegion(region_id=_suffix_id("examples", region_suffix), code="\n".join(lines))


def _render_raises_cases(
    cases: tuple[CallCase, ...],
    *,
    declared: tuple[str, ...],
    region_suffix: str,
) -> DerivedRegion:
    if _all_plain_legacy(cases) and not region_suffix:
        # Legacy cases are parser-built as target(<input>), so slicing between
        # the first "(" and trailing ")" recovers the exact original input
        # expression before the old renderer produces today's bytes.
        rows = tuple(
            RaisesRow(c.call_expr[c.call_expr.index("(") + 1 : -1], c.exc_name or "") for c in cases
        )
        target = cases[0].call_expr[: cases[0].call_expr.index("(")]
        return _render_errors_region(rows, target)
    by_exc: dict[str, list[CallCase]] = {}
    for c in cases:
        by_exc.setdefault(c.exc_name or "Exception", []).append(c)
    blocks_out: list[str] = []
    for exc, exc_cases in by_exc.items():
        is_async = any(c.is_async for c in exc_cases)
        params = _fixture_params(tuple(exc_cases), declared)
        prefix = "async def" if is_async else "def"
        fn_name = _suffix_fn(f"test_raises_{exc.lower()}", region_suffix)
        lines = []
        if is_async:
            lines.append("@pytest.mark.asyncio")
        lines.append(f"{prefix} {fn_name}({params}):  # derived from: Raises")
        for c in exc_cases:
            call = f"await {c.call_expr}" if c.is_async else c.call_expr
            lines.append(f"    with pytest.raises({exc}):")
            lines.append(f"        {call}")
        blocks_out.append("\n".join(lines))
    return DerivedRegion(
        region_id=_suffix_id("errors", region_suffix), code="\n\n".join(blocks_out)
    )


def derive_case_regions(
    blocks: CaseBlocks,
    *,
    target: str,
    derive: list[str],
    region_suffix: str = "",
) -> list[DerivedRegion]:
    regions: list[DerivedRegion] = []
    if "examples" in derive and blocks.examples:
        regions.append(
            _render_examples_cases(
                blocks.examples, declared=blocks.fixtures_declared, region_suffix=region_suffix
            )
        )
    if "errors" in derive and blocks.raises:
        regions.append(
            _render_raises_cases(
                blocks.raises, declared=blocks.fixtures_declared, region_suffix=region_suffix
            )
        )
    return regions


def evaluate_cases(blocks: CaseBlocks, *, namespace: dict[str, object]) -> list[str]:
    """Run pure derived cases in-process. Fixture cases are skipped (validated
    by the battery pytest run at reconcile time). Async calls are driven with
    asyncio.run."""

    import asyncio
    import inspect

    def _run(expr: str) -> object:
        got = eval(expr, dict(namespace))  # noqa: S307 - exprs come from the contract docstring
        if inspect.iscoroutine(got):
            got = asyncio.run(got)
        return got

    failures: list[str] = []
    for case in blocks.examples:
        if case.fixtures:
            continue
        try:
            got = _run(case.call_expr)
            want = eval(case.expected_expr or "None", dict(namespace))  # noqa: S307
            if got != want:
                failures.append(f"example {case.source_line} -> {got!r}, expected {want!r}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"example {case.source_line} raised {type(exc).__name__}: {exc}")
    for case in blocks.raises:
        if case.fixtures:
            continue
        exc_type = _resolve_exc(case.exc_name or "Exception", namespace)
        try:
            _run(case.call_expr)
            failures.append(f"raises {case.source_line}: expected {case.exc_name}, none raised")
        except exc_type:
            pass
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"raises {case.source_line}: expected {case.exc_name}, got {type(exc).__name__}"
            )
    return failures
