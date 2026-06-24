"""Derive a structured contract from docstring prose and render it to pytest.

v1 is deterministic for structured `Examples:`/`Raises:` blocks (single positional
argument). The model fallback for unstructured prose is added in a later task.
"""

from __future__ import annotations

import ast
import builtins
import re
from dataclasses import dataclass

from jaunt.contracts.battery import DerivedRegion


@dataclass(frozen=True, slots=True)
class ExampleRow:
    input_expr: str
    expected_expr: str


@dataclass(frozen=True, slots=True)
class RaisesRow:
    input_expr: str
    exc_name: str


@dataclass(frozen=True, slots=True)
class ContractBlocks:
    examples: tuple[ExampleRow, ...] = ()
    raises: tuple[RaisesRow, ...] = ()

    def is_empty(self) -> bool:
        return not self.examples and not self.raises


_HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z ]*):\s*$")
_EXC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _section_lines(docstring: str, name: str) -> list[str]:
    """Return the bullet lines under a `Name:` header until the next header/blank."""

    lines = docstring.splitlines()
    out: list[str] = []
    collecting = False
    for raw in lines:
        line = raw.strip()
        m = _HEADER_RE.match(line)
        if m:
            collecting = m.group(1).strip().lower() == name.lower()
            continue
        if collecting:
            if not line:
                break
            if line.startswith("- "):
                out.append(line[2:].strip())
    return out


def _is_expr(text: str) -> bool:
    try:
        ast.parse(text, mode="eval")
        return True
    except SyntaxError:
        return False


def _parse_examples(docstring: str) -> tuple[ExampleRow, ...]:
    rows: list[ExampleRow] = []
    for line in _section_lines(docstring, "Examples"):
        if "->" not in line:
            continue
        left, right = line.split("->", 1)
        left, right = left.strip(), right.strip()
        if _is_expr(left) and _is_expr(right):
            rows.append(ExampleRow(left, right))
    return tuple(rows)


def _parse_raises(docstring: str) -> tuple[RaisesRow, ...]:
    rows: list[RaisesRow] = []
    for line in _section_lines(docstring, "Raises"):
        # Form A: "<input> raises <Exc>"
        if " raises " in line:
            inp, exc = line.split(" raises ", 1)
            inp, exc = inp.strip(), exc.strip().rstrip(".")
            if _is_expr(inp) and _EXC_NAME_RE.match(exc):
                rows.append(RaisesRow(inp, exc))
                continue
        # Form B: "<Exc> on <input>"
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+on\s+(.+)$", line)
        if m and _is_expr(m.group(2).strip()):
            rows.append(RaisesRow(m.group(2).strip(), m.group(1)))
    return tuple(rows)


def extract_blocks_structured(docstring: str) -> ContractBlocks:
    return ContractBlocks(examples=_parse_examples(docstring), raises=_parse_raises(docstring))


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


def derive_regions(
    blocks: ContractBlocks, *, func_name: str, derive: list[str]
) -> list[DerivedRegion]:
    regions: list[DerivedRegion] = []
    if "examples" in derive and blocks.examples:
        regions.append(_render_examples_region(blocks.examples, func_name))
    if "errors" in derive and blocks.raises:
        regions.append(_render_errors_region(blocks.raises, func_name))
    return regions


def _resolve_exc(name: str, namespace: dict[str, object]) -> type[BaseException]:
    obj = namespace.get(name, getattr(builtins, name, None))
    if isinstance(obj, type) and issubclass(obj, BaseException):
        return obj
    raise ValueError(f"Unknown exception type in contract: {name!r}")


def evaluate_blocks(fn: object, blocks: ContractBlocks, namespace: dict[str, object]) -> list[str]:
    """Run derived cases directly against `fn`. Returns failure descriptions."""

    failures: list[str] = []
    for row in blocks.examples:
        try:
            arg = eval(row.input_expr, dict(namespace))  # noqa: S307 - literal exprs from prose
            want = eval(row.expected_expr, dict(namespace))  # noqa: S307
            got = fn(arg)  # type: ignore[operator]
            if got != want:
                failures.append(f"example {row.input_expr} -> {got!r}, expected {want!r}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"example {row.input_expr} raised {type(exc).__name__}: {exc}")
    for row in blocks.raises:
        exc_type = _resolve_exc(row.exc_name, namespace)
        try:
            arg = eval(row.input_expr, dict(namespace))  # noqa: S307
            fn(arg)  # type: ignore[operator]
            failures.append(f"raises {row.input_expr}: expected {row.exc_name}, none raised")
        except exc_type:
            pass
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"raises {row.input_expr}: expected {row.exc_name}, got {type(exc).__name__}"
            )
    return failures
