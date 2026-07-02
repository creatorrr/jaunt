# Adoption Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `@jaunt.contract` / `jaunt adopt` / `reconcile` / `check` / `eject` from top-level sync single-arg functions to async functions, whole classes, and fixture-parameterized batteries.

**Architecture:** A new call-plan case grammar (`src/jaunt/contract/cases.py`) becomes the single IR feeding three consumers: the pytest battery renderer, in-process reconcile validation, and mutation-strength scoring. The contract digest path widens to async functions and classes via a contract-specific normalizer (plain sync-function digest inputs stay byte-identical). The battery pipeline gains async emission (pytest-asyncio auto mode), per-method class regions, and a fixture seam validated through a temp-sibling pytest run.

**Tech Stack:** Python 3.12+, stdlib `ast`, pytest + pytest-asyncio (both already base dependencies). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-02-adoption-parity-design.md` — read it before starting.

## Global Constraints

- Run everything with `uv run` from the repo root. Full gate: `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q`. All tests are mocked — no API keys, no codex calls.
- Ruff: line-length 100, rules E/F/I/UP/B. Run `uv run ruff format <files>` before every commit (CI auto-commits formatting otherwise).
- **Byte-compat invariant 1 (digests):** for a plain top-level sync function, the three strings hashed by `contract_digests` must be byte-identical to today's. Golden tests enforce this.
- **Byte-compat invariant 2 (batteries):** a docstring whose cases are all legacy sugar (`arg -> want`, `arg raises Exc`) on a plain sync function must render byte-identical battery region code to today's parametrize form.
- **No-write-on-failure invariant:** a failed `reconcile` must leave the battery file untouched (existing tests assert this; the fixture-validation temp file must be cleaned up in a `finally`).
- The pytest flags for battery runs are exactly: `-p pytest_asyncio -o asyncio_mode=auto` appended to the existing arg list in `run_battery_file`.
- Region ids: functions keep `examples` / `errors`; class methods use `examples-<method>` / `errors-<method>`.
- Commit after every task with the message given in its final step. Do not batch tasks into one commit.

---

### Task 1: Call-plan IR and case-expression parser

**Files:**
- Create: `src/jaunt/contract/cases.py`
- Test: `tests/test_contract_cases.py`

**Interfaces:**
- Consumes: nothing new (stdlib `ast` only).
- Produces (used by every later task):
  - `class CaseParseError(ValueError)` with attribute `line: str`
  - `@dataclass(frozen=True, slots=True) class CallCase: source_line: str; call_expr: str; expected_expr: str | None; exc_name: str | None; fixtures: tuple[str, ...]; imports: tuple[str, ...]; is_async: bool; legacy: bool; method: str | None`
  - `@dataclass(frozen=True, slots=True) class CaseBlocks: examples: tuple[CallCase, ...]; raises: tuple[CallCase, ...]; fixtures_declared: tuple[str, ...]` with methods `is_empty() -> bool`, `has_fixture_cases() -> bool`, and `merged(other: CaseBlocks) -> CaseBlocks` (concatenates examples/raises, unions fixtures_declared preserving first-seen order)
  - `def parse_case_blocks(docstring: str, *, target: str, async_map: dict[str, bool], module_names: frozenset[str], method: str | None = None) -> CaseBlocks`

`async_map` maps callables to async-ness: key `target` for a plain function (e.g. `{"slugify": False}`), key `"<Cls>.<method>"` for class methods (e.g. `{"Counter.increment": False, "Counter.aincrement": True}`). `module_names` is the set of top-level names defined in the adopted module (used to classify references; the target itself need not be in it).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_contract_cases.py`:

```python
"""Tests for the call-plan case grammar (contract mode adoption parity)."""

from __future__ import annotations

import pytest

from jaunt.contract.cases import CaseParseError, parse_case_blocks


def _parse(doc: str, **kw):
    defaults = dict(target="f", async_map={"f": False}, module_names=frozenset())
    defaults.update(kw)
    return parse_case_blocks(doc, **defaults)


class TestLegacySugar:
    def test_arrow_form_is_legacy_single_arg_call(self) -> None:
        blocks = _parse("Examples:\n    - 'a b' -> 'a-b'\n")
        assert len(blocks.examples) == 1
        case = blocks.examples[0]
        assert case.legacy is True
        assert case.call_expr == "f('a b')"
        assert case.expected_expr == "'a-b'"
        assert case.fixtures == ()
        assert case.imports == ()
        assert case.is_async is False

    def test_raises_sugar_forms(self) -> None:
        blocks = _parse("Raises:\n    - '' raises ValueError\n    - TypeError on 1\n")
        assert [(c.call_expr, c.exc_name) for c in blocks.raises] == [
            ("f('')", "ValueError"),
            ("f(1)", "TypeError"),
        ]
        assert all(c.legacy for c in blocks.raises)

    def test_unparseable_lines_are_skipped_like_today(self) -> None:
        # Legacy behavior: prose lines under Examples that are not parseable
        # cases are ignored, not errors (only explicit call-form lines error).
        blocks = _parse("Examples:\n    - lowercases everything\n")
        assert blocks.is_empty()


class TestCallForm:
    def test_multi_arg_kwargs_call(self) -> None:
        blocks = _parse("Examples:\n    - f([1, 2], sep='-') == '1-2'\n")
        case = blocks.examples[0]
        assert case.legacy is False
        assert case.call_expr == "f([1, 2], sep='-')"
        assert case.expected_expr == "'1-2'"

    def test_constructor_recipe_method_chain(self) -> None:
        blocks = _parse(
            "Examples:\n    - Counter(start=10).increment(5) == 15\n",
            target="Counter",
            async_map={"Counter.increment": False},
        )
        case = blocks.examples[0]
        assert case.call_expr == "Counter(start=10).increment(5)"
        assert case.method == "increment"

    def test_call_form_raises(self) -> None:
        blocks = _parse(
            "Raises:\n    - Counter(start=-1) raises ValueError\n",
            target="Counter",
            async_map={},
        )
        assert blocks.raises[0].call_expr == "Counter(start=-1)"
        assert blocks.raises[0].exc_name == "ValueError"

    def test_async_flag_from_async_map(self) -> None:
        blocks = _parse("Examples:\n    - f(1) == 2\n", async_map={"f": True})
        assert blocks.examples[0].is_async is True

    def test_async_method_flag(self) -> None:
        blocks = _parse(
            "Examples:\n    - C().go(1) == 2\n",
            target="C",
            async_map={"C.go": True},
        )
        assert blocks.examples[0].is_async is True
        assert blocks.examples[0].method == "go"


class TestNameClassification:
    def test_module_level_name_becomes_import(self) -> None:
        blocks = _parse(
            "Examples:\n    - f('alice') == User('alice')\n",
            module_names=frozenset({"User"}),
        )
        assert blocks.examples[0].imports == ("User",)

    def test_builtin_names_are_allowed_without_import(self) -> None:
        blocks = _parse("Examples:\n    - f(len('ab')) == 2\n")
        assert blocks.examples[0].imports == ()

    def test_unknown_name_is_parse_error_with_line(self) -> None:
        with pytest.raises(CaseParseError) as ei:
            _parse("Examples:\n    - f(mystery) == 1\n")
        assert "mystery" in str(ei.value)
        assert ei.value.line == "f(mystery) == 1"


class TestFixtures:
    def test_fixtures_line_declares_names(self) -> None:
        doc = "Examples:\n    - f(db, 'alice') == 1\n\nFixtures: db\n"
        blocks = _parse(doc)
        assert blocks.fixtures_declared == ("db",)
        assert blocks.examples[0].fixtures == ("db",)
        assert blocks.has_fixture_cases() is True

    def test_declared_but_unused_fixture_is_fine(self) -> None:
        doc = "Examples:\n    - f(1) == 2\n\nFixtures: db\n"
        blocks = _parse(doc)
        assert blocks.fixtures_declared == ("db",)
        assert blocks.examples[0].fixtures == ()
        assert blocks.has_fixture_cases() is False


class TestMerged:
    def test_merged_concatenates_and_unions(self) -> None:
        a = _parse("Examples:\n    - f(1) == 1\n\nFixtures: db\n")
        b = _parse("Raises:\n    - f('') raises ValueError\n\nFixtures: tmp_path, db\n")
        m = a.merged(b)
        assert len(m.examples) == 1 and len(m.raises) == 1
        assert m.fixtures_declared == ("db", "tmp_path")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_contract_cases.py -q`
Expected: FAIL at import time — `ModuleNotFoundError: No module named 'jaunt.contract.cases'`

- [ ] **Step 3: Implement `src/jaunt/contract/cases.py`**

```python
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

_HEADER_RE = re.compile(r"([A-Za-z][A-Za-z ]*):\s*$")
_EXC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FIXTURES_RE = re.compile(r"^Fixtures:\s*(.+)$", re.MULTILINE)
_BUILTIN_NAMES = frozenset(dir(builtins))


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


def _section_lines(docstring: str, name: str) -> list[str]:
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
    return tuple(n for n in names if n and _EXC_NAME_RE.match(n))


def _is_expr(text: str) -> bool:
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
    kw = dict(
        target=target,
        async_map=async_map,
        fixtures_declared=fixtures_declared,
        module_names=module_names,
        method_override=method,
    )

    examples: list[CallCase] = []
    for line in _section_lines(docstring, "Examples"):
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
            if _is_expr(left) and _is_expr(right):
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
    for line in _section_lines(docstring, "Raises"):
        if " raises " in line:
            inp, exc = line.split(" raises ", 1)
            inp, exc = inp.strip(), exc.strip().rstrip(".")
            if not (_is_expr(inp) and _EXC_NAME_RE.match(exc)):
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
        if m and _is_expr(m.group(2).strip()):
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract_cases.py -q`
Expected: PASS (all).

- [ ] **Step 5: Run the full gate and commit**

Run: `uv run ruff format src/jaunt/contract/cases.py tests/test_contract_cases.py && uv run ruff check . && uv run ty check && uv run pytest -q`
Expected: everything green (existing suite untouched).

```bash
git add src/jaunt/contract/cases.py tests/test_contract_cases.py
git commit -m "feat(contract): call-plan case grammar — multi-arg, constructor recipes, fixtures, async flags"
```

---

### Task 2: Battery renderer over the IR (legacy byte-compat + general form)

**Files:**
- Modify: `src/jaunt/contract/derive.py` (add new renderer functions; keep the old ones untouched for now)
- Modify: `src/jaunt/contract/battery.py` (add `extra_imports` parameter)
- Test: `tests/test_contract_cases.py` (append render tests)

**Interfaces:**
- Consumes: `CaseBlocks`, `CallCase` from Task 1; `DerivedRegion` from `jaunt.contract.battery`.
- Produces:
  - `def derive_case_regions(blocks: CaseBlocks, *, target: str, derive: list[str], region_suffix: str = "") -> list[DerivedRegion]` in `derive.py`. `region_suffix=""` yields ids `examples`/`errors`; `region_suffix="increment"` yields `examples-increment`/`errors-increment`.
  - `def battery_extra_imports(blocks: CaseBlocks) -> tuple[str, ...]` in `derive.py` (sorted union of `case.imports`).
  - `render_battery(...)` / `merge_battery(...)` in `battery.py` gain keyword-only `extra_imports: tuple[str, ...] = ()`, emitted as one `from {import_module} import {name}` line per name, after the base import line.

**Rendering rules (copy into the implementation as its docstring):**
1. If **every** example case is `legacy` and none is async or fixture-using: emit today's exact parametrize region (byte-identical). Same rule, separately, for raises.
2. Otherwise emit the general form. Examples:

```python
def test_examples(db):  # derived from: Examples
    assert f(db, 'alice') == 1
    assert f([1, 2], sep='-') == '1-2'
```

Async examples (any case async ⇒ test is async; each async call awaited):

```python
async def test_examples():  # derived from: Examples
    assert await f(1) == 2
```

General raises (one test per exception name, suffix `-<method>` handled by region id, fixture params joined):

```python
def test_raises_valueerror():  # derived from: Raises
    with pytest.raises(ValueError):
        Counter(start=-1)
```

Fixture params: union of `case.fixtures` across the cases in that region, in first-declared order.

- [ ] **Step 1: Append failing render tests to `tests/test_contract_cases.py`**

```python
from jaunt.contract.derive import (  # noqa: E402
    battery_extra_imports,
    derive_case_regions,
    derive_regions,
    extract_blocks_structured,
)


class TestRenderRegions:
    def test_all_legacy_renders_byte_identical_to_today(self) -> None:
        doc = "Examples:\n    - 'a b' -> 'a-b'\n\nRaises:\n    - 1 raises TypeError\n"
        old = derive_regions(
            extract_blocks_structured(doc), func_name="f", derive=["examples", "errors"]
        )
        new = derive_case_regions(
            _parse(doc), target="f", derive=["examples", "errors"]
        )
        assert [(r.region_id, r.code) for r in new] == [(r.region_id, r.code) for r in old]

    def test_general_form_multi_arg(self) -> None:
        blocks = _parse("Examples:\n    - f([1, 2], sep='-') == '1-2'\n")
        [region] = derive_case_regions(blocks, target="f", derive=["examples"])
        assert region.region_id == "examples"
        assert "def test_examples():  # derived from: Examples" in region.code
        assert "assert f([1, 2], sep='-') == '1-2'" in region.code

    def test_async_examples_awaited(self) -> None:
        blocks = _parse("Examples:\n    - f(1) == 2\n", async_map={"f": True})
        [region] = derive_case_regions(blocks, target="f", derive=["examples"])
        assert "async def test_examples():" in region.code
        assert "assert await f(1) == 2" in region.code

    def test_fixture_params(self) -> None:
        doc = "Examples:\n    - f(db, 'a') == 1\n\nFixtures: db\n"
        [region] = derive_case_regions(_parse(doc), target="f", derive=["examples"])
        assert "def test_examples(db):" in region.code

    def test_region_suffix_for_methods(self) -> None:
        blocks = _parse(
            "Examples:\n    - C().go(1) == 2\n", target="C", async_map={"C.go": False}
        )
        [region] = derive_case_regions(
            blocks, target="C", derive=["examples"], region_suffix="go"
        )
        assert region.region_id == "examples-go"
        assert "def test_examples_go():" in region.code

    def test_general_raises(self) -> None:
        blocks = _parse(
            "Raises:\n    - C(start=-1) raises ValueError\n", target="C", async_map={}
        )
        [region] = derive_case_regions(blocks, target="C", derive=["errors"])
        assert "with pytest.raises(ValueError):" in region.code
        assert "C(start=-1)" in region.code

    def test_extra_imports_union(self) -> None:
        doc = "Examples:\n    - f('a') == User('a')\n    - f('b') == User('b')\n"
        blocks = _parse(doc, module_names=frozenset({"User"}))
        assert battery_extra_imports(blocks) == ("User",)


class TestBatteryExtraImports:
    def test_render_battery_emits_one_line_per_extra_import(self) -> None:
        from jaunt.contract.battery import render_battery

        text = render_battery(
            import_module="m",
            func_name="f",
            regions=[],
            header_fields={
                "derived_from": "m:f",
                "prose_digest": "0" * 64,
                "signature": "sha256:" + "0" * 64,
                "body_digest": "0" * 64,
                "strength": "0/0",
                "tool_version": "test",
            },
            extra_imports=("User",),
        )
        assert "from m import f\nfrom m import User\n" in text

    def test_no_extra_imports_is_byte_identical(self) -> None:
        from jaunt.contract.battery import render_battery

        kw = dict(
            import_module="m",
            func_name="f",
            regions=[],
            header_fields={
                "derived_from": "m:f",
                "prose_digest": "0" * 64,
                "signature": "sha256:" + "0" * 64,
                "body_digest": "0" * 64,
                "strength": "0/0",
                "tool_version": "test",
            },
        )
        assert render_battery(**kw) == render_battery(**kw, extra_imports=())
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contract_cases.py -q`
Expected: FAIL — `ImportError: cannot import name 'derive_case_regions'`

- [ ] **Step 3: Implement the renderer in `derive.py`**

Append to `src/jaunt/contract/derive.py`:

```python
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
    lines = [f"{prefix} {fn_name}({params}):  # derived from: Examples"]
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
        rows = tuple(
            RaisesRow(c.call_expr[c.call_expr.index("(") + 1 : -1], c.exc_name or "")
            for c in cases
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
        lines = [f"{prefix} {fn_name}({params}):  # derived from: Raises"]
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
```

Note the legacy-path trick: a legacy case's `call_expr` is always `target(<input>)` built by the parser, so slicing between the first `(` and the trailing `)` recovers the original input expression exactly, and `_render_examples_region`/`_render_errors_region` then produce today's bytes. `target` is likewise recovered from the prefix. (Add this as a comment.)

In `src/jaunt/contract/battery.py`, replace `render_battery` and `merge_battery` in full:

```python
def render_battery(
    *,
    import_module: str,
    func_name: str,
    regions: list[DerivedRegion],
    header_fields: dict[str, str],
    preserved: str = "",
    extra_imports: tuple[str, ...] = (),
) -> str:
    parts = [
        _header_text(header_fields).rstrip(),
        "import pytest",
        f"from {import_module} import {func_name}",
    ]
    parts += [f"from {import_module} import {name}" for name in extra_imports]
    parts.append("")
    for region in regions:
        parts.append(_region_block(region))
        parts.append("")
    body = "\n".join(parts).rstrip() + "\n"
    if preserved.strip():
        body += "\n\n" + preserved.strip() + "\n"
    return body
```

```python
def merge_battery(
    existing: str | None,
    *,
    import_module: str,
    func_name: str,
    regions: list[DerivedRegion],
    header_fields: dict[str, str],
    extra_imports: tuple[str, ...] = (),
) -> str:
    preserved = ""
    if existing is not None:
        preserved = parse_battery(existing).preserved
    return render_battery(
        import_module=import_module,
        func_name=func_name,
        regions=regions,
        header_fields=header_fields,
        preserved=preserved,
        extra_imports=extra_imports,
    )
```

(The preamble stripper in `parse_battery` already skips `from X import Y` single-name lines, so extra import lines merge cleanly.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_contract_cases.py tests/test_contract_battery.py -q`
Expected: PASS.

- [ ] **Step 5: Full gate + commit**

Run: `uv run ruff format src/jaunt/contract/ tests/test_contract_cases.py && uv run ruff check . && uv run ty check && uv run pytest -q`

```bash
git add src/jaunt/contract/derive.py src/jaunt/contract/battery.py tests/test_contract_cases.py
git commit -m "feat(contract): IR battery renderer — legacy byte-compat, general/async/fixture forms, extra imports"
```

---

### Task 3: In-process evaluator + strength over the IR

**Files:**
- Modify: `src/jaunt/contract/derive.py` (add `evaluate_cases`)
- Modify: `src/jaunt/contract/strength.py` (add `compute_case_strength`)
- Modify: `src/jaunt/header.py` (+ `strength-excluded` field)
- Test: `tests/test_contract_cases.py` (append)

**Interfaces:**
- Produces:
  - `def evaluate_cases(blocks: CaseBlocks, *, namespace: dict[str, object]) -> list[str]` in `derive.py`. Evaluates only **pure** cases (skips any case with `fixtures`); async call results detected with `inspect.iscoroutine` and driven by `asyncio.run`. `namespace` must contain the target and every name in `case.imports` (callers build it; builtins come from `eval` defaults). Returns failure strings shaped like today's (`"example <line> -> got, expected want"`).
  - `def compute_case_strength(source: str, target: str, blocks: CaseBlocks, namespace: dict[str, object]) -> tuple[int, int, int]` in `strength.py` returning `(killed, applicable, excluded)` where `excluded` counts fixture cases not scored. Reuses `iter_mutants` unchanged (it mutates any module source, classes included).
  - `format_contract_battery_header(..., strength_excluded: str = "0")` emits `# jaunt:strength-excluded=<K>` **only when K != "0"** (existing batteries stay byte-identical).

- [ ] **Step 1: Append failing tests**

```python
class TestEvaluateCases:
    def test_pure_example_pass_and_fail(self) -> None:
        from jaunt.contract.derive import evaluate_cases

        blocks = _parse("Examples:\n    - f(1, 2) == 3\n    - f(1, 2) == 4\n")
        failures = evaluate_cases(blocks, namespace={"f": lambda a, b: a + b})
        assert len(failures) == 1
        assert "expected 4" in failures[0]

    def test_async_case_run_via_asyncio(self) -> None:
        from jaunt.contract.derive import evaluate_cases

        async def f(x):
            return x + 1

        blocks = _parse("Examples:\n    - f(1) == 2\n", async_map={"f": True})
        assert evaluate_cases(blocks, namespace={"f": f}) == []

    def test_fixture_cases_are_skipped(self) -> None:
        from jaunt.contract.derive import evaluate_cases

        doc = "Examples:\n    - f(db) == 1\n\nFixtures: db\n"
        assert evaluate_cases(_parse(doc), namespace={"f": lambda db: 1}) == []

    def test_raises_case(self) -> None:
        from jaunt.contract.derive import evaluate_cases

        def f(x):
            if x == "":
                raise ValueError("empty")
            return x

        blocks = _parse("Raises:\n    - f('') raises ValueError\n")
        assert evaluate_cases(blocks, namespace={"f": f}) == []

    def test_class_constructor_case(self) -> None:
        from jaunt.contract.derive import evaluate_cases

        class Counter:
            def __init__(self, start=0):
                self.n = start

            def increment(self, by):
                self.n += by
                return self.n

        blocks = _parse(
            "Examples:\n    - Counter(start=10).increment(5) == 15\n",
            target="Counter",
            async_map={"Counter.increment": False},
        )
        assert evaluate_cases(blocks, namespace={"Counter": Counter}) == []


class TestCaseStrength:
    def test_strength_counts_and_exclusions(self) -> None:
        from jaunt.contract.strength import compute_case_strength

        src = "def f(a, b):\n    return a + b\n"
        doc = "Examples:\n    - f(1, 2) == 3\n    - f(db, 1) == 2\n\nFixtures: db\n"
        blocks = _parse(doc)
        killed, applicable, excluded = compute_case_strength(src, "f", blocks, {})
        assert excluded == 1
        assert applicable > 0
        assert killed > 0


class TestHeaderStrengthExcluded:
    def test_field_omitted_when_zero(self) -> None:
        from jaunt.header import format_contract_battery_header

        base = dict(
            derived_from="m:f",
            prose_digest="0" * 64,
            signature="sha256:" + "0" * 64,
            body_digest="0" * 64,
            strength="1/2",
            tool_version="t",
        )
        assert "strength-excluded" not in format_contract_battery_header(**base)
        out = format_contract_battery_header(**base, strength_excluded="2")
        assert "# jaunt:strength-excluded=2" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contract_cases.py -q`
Expected: FAIL — missing `evaluate_cases` / `compute_case_strength` / unexpected kwarg.

- [ ] **Step 3: Implement**

Append to `derive.py`:

```python
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
            failures.append(
                f"raises {case.source_line}: expected {case.exc_name}, none raised"
            )
        except exc_type:
            pass
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"raises {case.source_line}: expected {case.exc_name}, got {type(exc).__name__}"
            )
    return failures
```

Append to `strength.py`:

```python
def compute_case_strength(
    source: str,
    target: str,
    blocks: "CaseBlocks",
    namespace: dict[str, object],
) -> tuple[int, int, int]:
    """(killed, applicable, excluded) over the call-plan IR. Fixture cases are
    excluded from scoring (mutating + re-running pytest per mutant is unbounded
    for DB fixtures); the count is surfaced in the battery header."""

    from jaunt.contract.cases import CaseBlocks
    from jaunt.contract.derive import evaluate_cases

    excluded = sum(1 for c in (*blocks.examples, *blocks.raises) if c.fixtures)
    pure = CaseBlocks(
        examples=tuple(c for c in blocks.examples if not c.fixtures),
        raises=tuple(c for c in blocks.raises if not c.fixtures),
        fixtures_declared=blocks.fixtures_declared,
    )
    if pure.is_empty():
        applicable = sum(1 for _ in iter_mutants(source))
        return (0, applicable, excluded)

    killed = 0
    applicable = 0
    for mutant_src in iter_mutants(source):
        ns: dict[str, object] = dict(namespace)
        try:
            exec(compile(mutant_src, "<mutant>", "exec"), ns)  # noqa: S102
        except Exception:  # noqa: BLE001 - non-applicable mutant
            continue
        obj = ns.get(target)
        if not callable(obj):
            continue
        applicable += 1
        ns_eval = dict(ns)
        if evaluate_cases(pure, namespace=ns_eval):
            killed += 1
    return (killed, applicable, excluded)
```

In `header.py`, change `format_contract_battery_header`: add keyword-only `strength_excluded: str = "0"` and, after the `strength` line, `if strength_excluded != "0": lines.append(f"# jaunt:strength-excluded={strength_excluded}")` (insert before `tool-version` so the field order is header, derived-from, prose, signature, body, strength, [strength-excluded], tool-version).

In `battery.py`'s `_header_text`, pass through: `strength_excluded=header_fields.get("strength_excluded", "0")`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_contract_cases.py tests/test_contract_battery.py -q`
Expected: PASS.

- [ ] **Step 5: Full gate + commit**

```bash
git add src/jaunt/contract/derive.py src/jaunt/contract/strength.py src/jaunt/header.py src/jaunt/contract/battery.py tests/test_contract_cases.py
git commit -m "feat(contract): IR evaluator + strength with fixture exclusion; strength-excluded header field"
```

---

### Task 4: Digest layer — async + class nodes, contract-specific normalizer

**Files:**
- Modify: `src/jaunt/digest.py` (widen loader; branch `contract_digests`)
- Test: `tests/test_contract_digests.py` (create)

**Interfaces:**
- Produces:
  - `def load_contract_node(source_file: str, qualname: str) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef` — replaces `load_function_node` for contract use. Dotted qualnames raise `ValueError("Contract specs are top-level functions or whole classes; adopt the whole class instead of 'Cls.method'.")`. `load_function_node` stays as a thin wrapper (`node = load_contract_node(...)`; raise if not `ast.FunctionDef`) so nothing else breaks.
  - `contract_digests(source_file, qualname)` returns `ContractDigests` for all three node kinds:
    - **sync function:** byte-identical inputs to today (docstring / `ast.unparse(args) + " -> " + returns` / body-minus-docstring; **no stub elision**).
    - **async function:** same, except signature input is `"async " + ast.unparse(args) + " -> " + returns`.
    - **class:** prose = class docstring + `"\n\n"`-joined public (no leading `_`) method docstrings in source order, each prefixed `"<name>:\n"`; signature = JSON `{"bases": [...], "attributes": sorted names, "methods": {name: {"kind": "method"|"async_method", "signature": <function sig with async prefix rule>} for ALL methods}}` (sorted keys, compact separators); body = `"\n\n"`-joined `"<name>:\n" + <body-minus-docstring>` for all methods in source order, **no stub elision**.

- [ ] **Step 1: Write failing tests**

Create `tests/test_contract_digests.py`:

```python
"""Contract digest widening: async + class nodes, byte-compat for sync functions."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from jaunt.digest import ContractDigests, contract_digests, load_contract_node


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(tmp_path: Path, source: str) -> str:
    p = tmp_path / "m.py"
    p.write_text(source, encoding="utf-8")
    return str(p)


SYNC_FN = '''
def f(a: int, b: int = 2) -> int:
    """Add things.

    Examples:
        - f(1) -> 3
    """
    return a + b
'''

STUB_FN = '''
def g(a: int) -> int:
    """Stubby."""
    raise NotImplementedError
'''


class TestSyncByteCompat:
    def test_sync_function_digests_are_golden(self, tmp_path: Path) -> None:
        src_file = _write(tmp_path, SYNC_FN)
        digs = contract_digests(src_file, "f")
        node = ast.parse(SYNC_FN).body[0]
        prose = ast.get_docstring(node, clean=True) or ""
        sig = ast.unparse(node.args) + " -> " + ast.unparse(node.returns)
        body = "\n".join(ast.unparse(s) for s in node.body[1:])
        assert digs == ContractDigests(prose=_sha(prose), signature=_sha(sig), body=_sha(body))

    def test_stub_body_is_hashed_not_elided(self, tmp_path: Path) -> None:
        src_file = _write(tmp_path, STUB_FN)
        digs = contract_digests(src_file, "g")
        assert digs.body == _sha("raise NotImplementedError")
        assert digs.body != _sha("")


class TestAsync:
    def test_async_signature_has_prefix_and_flip_changes_digest(self, tmp_path: Path) -> None:
        sync_file = _write(tmp_path, "def f(a: int) -> int:\n    return a\n")
        sync_digs = contract_digests(sync_file, "f")
        (tmp_path / "m.py").write_text(
            "async def f(a: int) -> int:\n    return a\n", encoding="utf-8"
        )
        async_digs = contract_digests(str(tmp_path / "m.py"), "f")
        assert sync_digs.signature != async_digs.signature
        assert sync_digs.body == async_digs.body

    def test_loader_returns_async_node(self, tmp_path: Path) -> None:
        src_file = _write(tmp_path, "async def f() -> None:\n    pass\n")
        assert isinstance(load_contract_node(src_file, "f"), ast.AsyncFunctionDef)


CLASS_SRC = '''
class Counter:
    """Counts things."""

    start = 0

    def __init__(self, start: int = 0) -> None:
        self.n = start

    def increment(self, by: int) -> int:
        """Bump.

        Examples:
            - Counter().increment(1) == 1
        """
        self.n += by
        return self.n

    async def aincrement(self, by: int) -> int:
        """Async bump."""
        return self.n + by

    def _private(self) -> None:
        pass
'''


class TestClassDigests:
    def test_loader_returns_class_node(self, tmp_path: Path) -> None:
        src_file = _write(tmp_path, CLASS_SRC)
        assert isinstance(load_contract_node(src_file, "Counter"), ast.ClassDef)

    def test_dotted_qualname_rejected(self, tmp_path: Path) -> None:
        src_file = _write(tmp_path, CLASS_SRC)
        with pytest.raises(ValueError, match="whole class"):
            load_contract_node(src_file, "Counter.increment")

    def test_method_docstring_edit_changes_prose_not_signature(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path, CLASS_SRC)
        d1 = contract_digests(f1, "Counter")
        (tmp_path / "m.py").write_text(CLASS_SRC.replace("Bump.", "Bump twice."), "utf-8")
        d2 = contract_digests(str(tmp_path / "m.py"), "Counter")
        assert d1.prose != d2.prose
        assert d1.signature == d2.signature
        assert d1.body == d2.body

    def test_private_method_docstring_not_in_prose(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path, CLASS_SRC)
        d1 = contract_digests(f1, "Counter")
        edited = CLASS_SRC.replace('def _private(self) -> None:\n        pass',
                                   'def _private(self) -> None:\n        """Doc."""\n        pass')
        (tmp_path / "m.py").write_text(edited, "utf-8")
        d2 = contract_digests(str(tmp_path / "m.py"), "Counter")
        assert d1.prose == d2.prose  # private docstring invisible to prose
        assert d1.body != d2.body    # but the body changed

    def test_method_add_changes_signature(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path, CLASS_SRC)
        d1 = contract_digests(f1, "Counter")
        (tmp_path / "m.py").write_text(
            CLASS_SRC + "\n    def reset(self) -> None:\n        self.n = 0\n"
            if False else CLASS_SRC.rstrip() + "\n\n    def reset(self) -> None:\n        self.n = 0\n",
            "utf-8",
        )
        d2 = contract_digests(str(tmp_path / "m.py"), "Counter")
        assert d1.signature != d2.signature

    def test_body_only_edit_changes_body_only(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path, CLASS_SRC)
        d1 = contract_digests(f1, "Counter")
        (tmp_path / "m.py").write_text(
            CLASS_SRC.replace("self.n += by", "self.n = self.n + by"), "utf-8"
        )
        d2 = contract_digests(str(tmp_path / "m.py"), "Counter")
        assert d1.body != d2.body
        assert d1.signature == d2.signature
        assert d1.prose == d2.prose
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contract_digests.py -q`
Expected: FAIL — `ImportError: cannot import name 'load_contract_node'`

- [ ] **Step 3: Implement in `digest.py`**

Replace `load_function_node` and `contract_digests` (keep `ContractDigests` as-is):

```python
def load_contract_node(
    source_file: str, qualname: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef:
    """Load a top-level function (sync or async) or class node by name."""

    if "." in qualname:
        raise ValueError(
            f"Contract specs are top-level functions or whole classes; "
            f"adopt the whole class instead of {qualname!r}."
        )
    src = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=source_file)
    for top in tree.body:
        if (
            isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and top.name == qualname
        ):
            return top
    raise ValueError(f"Top-level function or class {qualname!r} not found in {source_file}.")


def load_function_node(source_file: str, qualname: str) -> ast.FunctionDef:
    """Back-compat shim: contract loader restricted to sync functions."""

    node = load_contract_node(source_file, qualname)
    if not isinstance(node, ast.FunctionDef):
        raise ValueError(f"{qualname!r} is not a top-level sync function.")
    return node


def _contract_fn_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    # Sync rendering must stay byte-identical to the historical form.
    sig = ast.unparse(node.args) + " -> " + (ast.unparse(node.returns) if node.returns else "")
    if isinstance(node, ast.AsyncFunctionDef):
        return "async " + sig
    return sig


def _contract_fn_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    # NO stub-body elision here: adopted code is real code, and eliding would
    # change existing digests (see the adoption-parity design spec).
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def _contract_class_inputs(node: ast.ClassDef) -> tuple[str, str, str]:
    """(prose, signature, body) strings for a whole-class contract."""

    methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    prose_parts = [ast.get_docstring(node, clean=True) or ""]
    for m in methods:
        if m.name.startswith("_"):
            continue
        doc = ast.get_docstring(m, clean=True) or ""
        if doc:
            prose_parts.append(f"{m.name}:\n{doc}")
    prose = "\n\n".join(prose_parts)

    attributes: list[str] = []
    for child in node.body:
        if isinstance(child, ast.Assign):
            attributes += [t.id for t in child.targets if isinstance(t, ast.Name)]
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            attributes.append(child.target.id)
    shape = {
        "bases": [ast.unparse(b) for b in node.bases],
        "attributes": sorted(attributes),
        "methods": {
            m.name: {
                "kind": "async_method" if isinstance(m, ast.AsyncFunctionDef) else "method",
                "signature": _contract_fn_signature(m),
            }
            for m in methods
        },
    }
    signature = json.dumps(shape, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    body = "\n\n".join(f"{m.name}:\n{_contract_fn_body(m)}" for m in methods)
    return prose, signature, body


def contract_digests(source_file: str, qualname: str) -> ContractDigests:
    """Stable prose/signature/body digests for a contract function or class."""

    node = load_contract_node(source_file, qualname)
    if isinstance(node, ast.ClassDef):
        prose, sig, body_src = _contract_class_inputs(node)
    else:
        prose = ast.get_docstring(node, clean=True) or ""
        sig = _contract_fn_signature(node)
        body_src = _contract_fn_body(node)
    return ContractDigests(prose=_sha(prose), signature=_sha(sig), body=_sha(body_src))
```

- [ ] **Step 4: Run tests — new file AND the whole suite** (byte-compat means existing contract tests must still pass unmodified)

Run: `uv run pytest tests/test_contract_digests.py -q && uv run pytest -q`
Expected: all PASS. If any existing contract test fails, the sync rendering broke byte-compat — fix the rendering, do not touch the old test.

- [ ] **Step 5: Full gate + commit**

```bash
git add src/jaunt/digest.py tests/test_contract_digests.py
git commit -m "feat(contract): digest layer widened to async functions + whole classes, sync bytes unchanged"
```

---

### Task 5: Runtime gate — admit async functions and classes

**Files:**
- Modify: `src/jaunt/runtime.py:479-525` (the `contract` decorator)
- Test: `tests/test_contract_runtime.py` (create)

**Interfaces:**
- Produces: `@jaunt.contract` accepts top-level classes and async functions. Still rejects: `classmethod`/`staticmethod`, methods (dotted `__qualname__` on functions), nested classes (dotted `__qualname__` on types). Registered `SpecEntry` unchanged in shape (`kind="contract"`, `class_name` left unset — class identity is `isinstance(entry.obj, type)`).

- [ ] **Step 1: Write failing tests**

Create `tests/test_contract_runtime.py`:

```python
"""@jaunt.contract runtime gate: async + whole-class admission."""

from __future__ import annotations

import pytest

import jaunt
from jaunt import registry
from jaunt.errors import JauntError


@pytest.fixture(autouse=True)
def _clean_registries():
    registry.clear_registries()
    yield
    registry.clear_registries()


def test_async_function_is_registered() -> None:
    @jaunt.contract
    async def fetch(x: int) -> int:
        """Fetch."""
        return x

    entries = registry.get_contract_registry()
    assert any(e.qualname == "fetch" for e in entries.values())


def test_class_is_registered() -> None:
    @jaunt.contract
    class Counter:
        """Counts."""

        def bump(self) -> int:
            return 1

    entries = registry.get_contract_registry()
    [entry] = [e for e in entries.values() if e.qualname == "Counter"]
    assert isinstance(entry.obj, type)


def test_method_still_rejected() -> None:
    with pytest.raises(JauntError, match="whole class"):

        class C:
            @jaunt.contract
            def m(self) -> None: ...


def test_staticmethod_still_rejected() -> None:
    with pytest.raises(JauntError):

        class C:
            @jaunt.contract
            @staticmethod
            def s() -> None: ...
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contract_runtime.py -q`
Expected: `test_async_function_is_registered` and `test_class_is_registered` FAIL with `JauntError` (the v1 gates).

- [ ] **Step 3: Rewrite the gate in `runtime.py`**

Replace the body of `_decorate` inside `contract` (keep the surrounding overloads and docstring; update the docstring's "top-level sync only" phrasing to "top-level functions — sync or async — and whole classes"):

```python
    def _decorate(fn: F) -> F:
        if isinstance(fn, (classmethod, staticmethod)):
            raise JauntError(
                "@contract must decorate a plain function or class "
                "(adopt the whole class, not a classmethod/staticmethod)."
            )
        if isinstance(fn, type):
            if "." in fn.__qualname__:
                raise JauntError("@contract supports top-level classes only (not nested classes).")
        else:
            class_name = _classify_qualname(fn)  # rejects closures/deep nesting
            if class_name is not None:
                raise JauntError(
                    "@contract does not support methods; adopt the whole class instead."
                )

        f = cast(Any, fn)
        qualname = cast(str, f.__qualname__)
        spec_ref = spec_ref_from_object(fn)

        decorator_kwargs: dict[str, object] = {}
        if deps is not None:
            decorator_kwargs["deps"] = deps

        entry = SpecEntry(
            kind="contract",
            spec_ref=spec_ref,
            module=cast(str, f.__module__),
            qualname=qualname,
            source_file=_source_file(fn),
            obj=fn,
            decorator_kwargs=decorator_kwargs,
        )
        register_contract(entry)
        return fn
```

(The `inspect.iscoroutinefunction` check is deleted outright; the tail of `_decorate` is unchanged from today.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_contract_runtime.py -q && uv run pytest -q`
Expected: PASS. Any existing test asserting the old async/class rejection message must be updated to the new admission behavior — check `grep -rn "does not support" tests/` and update expectations to match the new messages.

- [ ] **Step 5: Full gate + commit**

```bash
git add src/jaunt/runtime.py tests/test_contract_runtime.py tests/
git commit -m "feat(contract): @jaunt.contract admits async functions and whole classes"
```

---

### Task 6: Marker editor — async and class nodes

**Files:**
- Modify: `src/jaunt/contract/edits.py`
- Test: `tests/test_contract_edits.py` (append; the file exists — if it does not, create it with only the new tests)

**Interfaces:**
- Produces: `_find_func` becomes `_find_target(source, name) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef` matching all three node types at module top level; error message: `f"top-level function or class {name!r} not found"`. `add_contract_marker` / `remove_contract_marker` keep their signatures and work for all three node types (the insertion logic is already node-generic: it uses `decorator_list` and `lineno`).

- [ ] **Step 1: Write failing tests**

```python
from jaunt.contract.edits import add_contract_marker, remove_contract_marker


class TestAsyncAndClassMarkers:
    def test_add_marker_async_function(self) -> None:
        src = "async def f(x):\n    return x\n"
        out = add_contract_marker(src, "f")
        assert "@jaunt.contract\nasync def f(x):" in out
        assert out.startswith("import jaunt")

    def test_add_marker_class(self) -> None:
        src = "class C:\n    def m(self):\n        return 1\n"
        out = add_contract_marker(src, "C")
        assert "@jaunt.contract\nclass C:" in out

    def test_add_marker_class_above_existing_decorator(self) -> None:
        src = "import functools\n\n@functools.total_ordering\nclass C:\n    pass\n"
        out = add_contract_marker(src, "C")
        assert "@jaunt.contract\n@functools.total_ordering\nclass C:" in out

    def test_remove_marker_class_roundtrip(self) -> None:
        src = "class C:\n    def m(self):\n        return 1\n"
        marked = add_contract_marker(src, "C")
        assert remove_contract_marker(marked, "C").replace("import jaunt\n", "") == src

    def test_missing_name_error_mentions_class(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="function or class"):
            add_contract_marker("x = 1\n", "nope")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contract_edits.py -q`
Expected: FAIL — async/class targets raise `top-level function ... not found`.

- [ ] **Step 3: Implement**

In `edits.py`, replace `_find_func`:

```python
def _find_target(
    source: str, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef:
    tree = ast.parse(source)
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            return node
    raise ValueError(f"top-level function or class {name!r} not found")
```

and change both `add_contract_marker` and `remove_contract_marker` to call `_find_target` (rename their local variable; the rest of both functions is already node-kind-agnostic).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_contract_edits.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/contract/edits.py tests/test_contract_edits.py
git commit -m "feat(contract): adopt/eject marker edits for async functions and classes"
```

---

### Task 7: Battery runner — filename sanitization + pytest-asyncio flags

**Files:**
- Modify: `src/jaunt/contract/runner.py` (`battery_path`, `run_battery_file`)
- Test: `tests/test_contract_runner.py` (append; create if absent)

**Interfaces:**
- Produces:
  - `battery_path` uses `f"test_{entry.qualname.replace('.', '_')}.py"` (defensive; contract qualnames are undotted after Task 4/5, but a sanitized path never produces `test_Cls.method.py`).
  - `run_battery_file` appends exactly `"-p", "pytest_asyncio", "-o", "asyncio_mode=auto"` to its subprocess arg list (after `--import-mode=importlib`).

- [ ] **Step 1: Write failing tests**

```python
from pathlib import Path

from jaunt.contract.runner import battery_path, run_battery_file
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef


def _entry(qualname: str) -> SpecEntry:
    return SpecEntry(
        kind="contract",
        spec_ref=SpecRef(module="pkg.mod", qualname=qualname),
        module="pkg.mod",
        qualname=qualname,
        source_file="src/pkg/mod.py",
        obj=None,
        decorator_kwargs={},
    )


def test_battery_path_sanitizes_dots(tmp_path: Path) -> None:
    p = battery_path(tmp_path, "tests/contract", _entry("Cls.method"))
    assert p.name == "test_Cls_method.py"


def test_async_battery_runs_green(tmp_path: Path) -> None:
    battery = tmp_path / "test_async_case.py"
    battery.write_text(
        "async def test_ok():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    assert run_battery_file(battery, root=tmp_path, source_roots=[]) is True


def test_async_battery_failure_detected(tmp_path: Path) -> None:
    battery = tmp_path / "test_async_fail.py"
    battery.write_text(
        "async def test_bad():\n    assert 1 + 1 == 3\n",
        encoding="utf-8",
    )
    assert run_battery_file(battery, root=tmp_path, source_roots=[]) is False
```

(Adapt the `SpecEntry`/`SpecRef` constructor kwargs to the real dataclass fields — check `src/jaunt/registry.py` and `src/jaunt/spec_ref.py` first; if `SpecRef` is constructed differently, build the entry the way `tests/test_contract_battery.py` already does.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contract_runner.py -q`
Expected: `test_battery_path_sanitizes_dots` FAILS (name contains a dot); `test_async_battery_runs_green` FAILS (async test not collected without the plugin flags — pytest reports it as skipped/warning, and with `-q` returncode 0/5 may actually pass vacuously: assert on the *failure* test instead — `test_async_battery_failure_detected` must be `False`, which without asyncio mode it will not be, because the test silently never runs).

- [ ] **Step 3: Implement**

In `runner.py`:

```python
def battery_path(root: Path, battery_dir: str, entry: SpecEntry) -> Path:
    parts = entry.module.split(".")
    fname = f"test_{entry.qualname.replace('.', '_')}.py"
    return root / battery_dir / Path(*parts) / fname
```

In `run_battery_file`, extend the subprocess args:

```python
        [
            sys.executable,
            "-m",
            "pytest",
            str(path),
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "--import-mode=importlib",
            "-p",
            "pytest_asyncio",
            "-o",
            "asyncio_mode=auto",
        ],
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_contract_runner.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/contract/runner.py tests/test_contract_runner.py
git commit -m "feat(contract): battery runner collects async tests; sanitize battery filenames"
```

---

### Task 8: Reconcile over the IR — functions (sync + async), fixture validation split

**Files:**
- Modify: `src/jaunt/contract/runner.py` (`reconcile_entry`)
- Test: `tests/test_contract_reconcile_parity.py` (create)

This is the load-bearing task: `reconcile_entry` switches from the legacy
`ContractBlocks` path to the IR for functions, adds the fixture-validation
split with a temp sibling, and threads `strength-excluded` + `extra_imports`.
Class handling arrives in Task 9 — until then `reconcile_entry` raises a clear
`ValueError` for class nodes.

**Interfaces:**
- Consumes: `parse_case_blocks`, `CaseBlocks` (Task 1); `derive_case_regions`, `battery_extra_imports`, `evaluate_cases` (Tasks 2–3); `compute_case_strength` (Task 3); `load_contract_node`, `contract_digests` (Task 4); flags (Task 7).
- Produces: `reconcile_entry` keeps its exact signature and `ReconcileResult` shape. New behavior contract:
  1. Build `async_map` and `module_names` from the source AST: `module_names = frozenset(top-level FunctionDef/AsyncFunctionDef/ClassDef/Assign-Name targets in the module file)`; for a function target, `async_map = {qualname: isinstance(node, ast.AsyncFunctionDef)}`.
  2. `blocks = parse_case_blocks(docstring, target=qualname, async_map=..., module_names=...)`. `CaseParseError` → `ReconcileResult(ok=False, failures=[f"{exc} (line: {exc.line})"], wrote=False)`.
  3. Legacy model fallback: when `blocks.is_empty()` and `model_extract` returns legacy `ContractBlocks`, convert each `ExampleRow`/`RaisesRow` through the sugar path (`parse_case_blocks` on a synthesized docstring: `"Examples:\n" + "\n".join(f"    - {r.input_expr} -> {r.expected_expr}") ...`).
  4. In-process namespace = `{qualname: module_namespace[qualname]}` plus `{name: module_namespace[name] for every import name used}`. Pure-case failures → abort, no write.
  5. Strength: `compute_case_strength(ast.unparse(node), qualname, blocks, namespace)`; header fields gain `"strength_excluded": str(excluded)`.
  6. Merged text built exactly as today plus `extra_imports=battery_extra_imports(blocks)`.
  7. **If `blocks.has_fixture_cases()`:** write merged text to `path.with_name(f"_jaunt_validate_{path.name}")` (note: no `test_` prefix, so a stray leftover is never collected by a project-wide pytest run), run it via `run_battery_file`, and in a `finally:` unlink the temp file. On pytest failure → `ReconcileResult(ok=False, failures=["fixture-dependent cases failed under pytest; run the battery for detail"], wrote=False)`. On success → `os.replace` is unnecessary (temp already unlinked); write the merged text to `path` as today.
  8. No fixture cases → write directly (today's behavior).

- [ ] **Step 1: Write failing tests**

Create `tests/test_contract_reconcile_parity.py`:

```python
"""reconcile_entry over the call-plan IR: async functions + fixture split."""

from __future__ import annotations

from pathlib import Path

from jaunt.contract.runner import reconcile_entry
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef


def _project(tmp_path: Path, module_src: str) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(module_src, encoding="utf-8")
    return tmp_path


def _entry(tmp_path: Path, qualname: str, obj) -> SpecEntry:
    return SpecEntry(
        kind="contract",
        spec_ref=SpecRef(module="mod", qualname=qualname),
        module="mod",
        qualname=qualname,
        source_file=str(tmp_path / "src" / "mod.py"),
        obj=obj,
        decorator_kwargs={},
    )


ASYNC_SRC = '''
async def double(x: int) -> int:
    """Double it.

    Examples:
        - double(2) == 4
    """
    return x * 2
'''


def test_async_function_reconciles_and_battery_is_async(tmp_path: Path) -> None:
    root = _project(tmp_path, ASYNC_SRC)

    async def double(x: int) -> int:
        return x * 2

    res = reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        False,
        _entry(root, "double", double),
        module_namespace={"double": double},
        tool_version="t",
    )
    assert res.ok, res.failures
    text = res.battery_path.read_text(encoding="utf-8")
    assert "async def test_examples():" in text
    assert "assert await double(2) == 4" in text


FIXTURE_SRC = '''
def lookup(db, key: str) -> str:
    """Look up.

    Examples:
        - lookup(db, 'a') == 'A'

    Fixtures: db
    """
    return db[key]
'''


def test_fixture_case_validated_via_pytest_and_written(tmp_path: Path) -> None:
    root = _project(tmp_path, FIXTURE_SRC)
    conftest_dir = root / "tests" / "contract" / "mod"
    conftest_dir.mkdir(parents=True)
    (root / "tests" / "contract" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef db():\n    return {'a': 'A'}\n",
        encoding="utf-8",
    )

    def lookup(db, key):
        return db[key]

    res = reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        False,
        _entry(root, "lookup", lookup),
        module_namespace={"lookup": lookup},
        tool_version="t",
    )
    assert res.ok, res.failures
    text = res.battery_path.read_text(encoding="utf-8")
    assert "def test_examples(db):" in text
    # No validation temp file left behind.
    leftovers = list(res.battery_path.parent.glob("_jaunt_validate_*"))
    assert leftovers == []


def test_fixture_case_failure_writes_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path, FIXTURE_SRC)
    (root / "tests" / "contract").mkdir(parents=True)
    (root / "tests" / "contract" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef db():\n    return {'a': 'WRONG'}\n",
        encoding="utf-8",
    )

    def lookup(db, key):
        return db[key]

    res = reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        False,
        _entry(root, "lookup", lookup),
        module_namespace={"lookup": lookup},
        tool_version="t",
    )
    assert res.ok is False
    assert not res.battery_path.exists()
    assert list(res.battery_path.parent.glob("_jaunt_validate_*")) == []


def test_case_parse_error_reports_line(tmp_path: Path) -> None:
    src = (
        'def f(x):\n    """F.\n\n    Examples:\n        - f(mystery) == 1\n    """\n'
        "    return x\n"
    )
    root = _project(tmp_path, src)
    res = reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        False,
        _entry(root, "f", lambda x: x),
        module_namespace={"f": lambda x: x},
        tool_version="t",
    )
    assert res.ok is False
    assert any("mystery" in f for f in res.failures)
    assert not res.battery_path.exists()


def test_strength_excluded_in_header(tmp_path: Path) -> None:
    root = _project(tmp_path, FIXTURE_SRC)
    (root / "tests" / "contract").mkdir(parents=True)
    (root / "tests" / "contract" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef db():\n    return {'a': 'A'}\n",
        encoding="utf-8",
    )

    def lookup(db, key):
        return db[key]

    res = reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        True,  # strength enabled
        _entry(root, "lookup", lookup),
        module_namespace={"lookup": lookup},
        tool_version="t",
    )
    assert res.ok, res.failures
    assert "# jaunt:strength-excluded=1" in res.battery_path.read_text(encoding="utf-8")
```

(As in Task 7, mirror the real `SpecEntry`/`SpecRef` construction from existing tests.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contract_reconcile_parity.py -q`
Expected: FAIL — async source raises the old "is async; unsupported" from any leftover `load_function_node` call, fixture tests fail on missing behavior.

- [ ] **Step 3: Rewrite `reconcile_entry`**

Replace the body of `reconcile_entry` in `runner.py`:

```python
def reconcile_entry(
    root: Path,
    battery_dir: str,
    derive: list[str],
    strength_enabled: bool,
    entry: SpecEntry,
    *,
    module_namespace: dict[str, object],
    tool_version: str,
    model_extract: Callable[[str], ContractBlocks] | None = None,
) -> ReconcileResult:
    import ast

    from jaunt.contract.battery import merge_battery
    from jaunt.contract.cases import CaseParseError, parse_case_blocks
    from jaunt.contract.derive import (
        battery_extra_imports,
        derive_case_regions,
        evaluate_cases,
    )
    from jaunt.contract.strength import compute_case_strength, format_strength
    from jaunt.digest import contract_digests, load_contract_node

    spec_ref = str(entry.spec_ref)
    path = battery_path(root, battery_dir, entry)

    node = load_contract_node(entry.source_file, entry.qualname)
    if isinstance(node, ast.ClassDef):
        # Task 9 wires whole-class reconcile; keep the error explicit until then.
        raise ValueError("whole-class reconcile not wired yet (adoption-parity Task 9)")

    module_names = _module_top_level_names(entry.source_file)
    async_map = {entry.qualname: isinstance(node, ast.AsyncFunctionDef)}
    docstring = _docstring_of(node)

    try:
        blocks = parse_case_blocks(
            docstring,
            target=entry.qualname,
            async_map=async_map,
            module_names=module_names,
        )
    except CaseParseError as exc:
        return ReconcileResult(
            spec_ref, False, "0/0", [f"{exc} (line: {exc.line})"], path, False
        )

    if blocks.is_empty() and model_extract is not None and docstring.strip():
        legacy = model_extract(docstring)
        blocks = parse_case_blocks(
            _legacy_blocks_to_docstring(legacy),
            target=entry.qualname,
            async_map=async_map,
            module_names=module_names,
        )

    fn = module_namespace.get(entry.qualname)
    if not callable(fn):
        return ReconcileResult(spec_ref, False, "0/0", ["function not importable"], path, False)

    eval_ns: dict[str, object] = {entry.qualname: fn}
    for case in (*blocks.examples, *blocks.raises):
        for name in case.imports:
            eval_ns[name] = module_namespace.get(name)

    failures = evaluate_cases(blocks, namespace=eval_ns)
    if failures:
        return ReconcileResult(spec_ref, False, "0/0", failures, path, False)

    digs = contract_digests(entry.source_file, entry.qualname)
    strength = "0/0"
    excluded = 0
    if strength_enabled:
        killed, applicable, excluded = compute_case_strength(
            ast.unparse(node), entry.qualname, blocks, eval_ns
        )
        strength = format_strength(killed, applicable)

    regions = derive_case_regions(blocks, target=entry.qualname, derive=derive)
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    text = merge_battery(
        existing,
        import_module=entry.module,
        func_name=entry.qualname,
        regions=regions,
        header_fields={
            "derived_from": spec_ref,
            "prose_digest": digs.prose,
            "signature": digs.signature,
            "body_digest": digs.body,
            "strength": strength,
            "strength_excluded": str(excluded),
            "tool_version": tool_version,
        },
        extra_imports=battery_extra_imports(blocks),
    )

    if blocks.has_fixture_cases():
        ok = _validate_via_pytest(text, path, root=root)
        if not ok:
            return ReconcileResult(
                spec_ref,
                False,
                "0/0",
                ["fixture-dependent cases failed under pytest; run the battery for detail"],
                path,
                False,
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ReconcileResult(spec_ref, True, strength, [], path, True)


def _module_top_level_names(source_file: str) -> frozenset[str]:
    import ast

    tree = ast.parse(Path(source_file).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return frozenset(names)


def _legacy_blocks_to_docstring(blocks: ContractBlocks) -> str:
    lines = []
    if blocks.examples:
        lines.append("Examples:")
        lines += [f"    - {r.input_expr} -> {r.expected_expr}" for r in blocks.examples]
        lines.append("")
    if blocks.raises:
        lines.append("Raises:")
        lines += [f"    - {r.input_expr} raises {r.exc_name}" for r in blocks.raises]
    return "\n".join(lines)


def _validate_via_pytest(text: str, path: Path, *, root: Path) -> bool:
    """Write the merged battery to a temp sibling (conftest discovery applies),
    run it, and always clean up. Non-`test_`-prefixed name so a stray leftover
    is never collected by a project-wide pytest run."""

    tmp = path.with_name(f"_jaunt_validate_{path.name}")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(text, encoding="utf-8")
        return run_battery_file(tmp, root=root, source_roots=[])
    finally:
        tmp.unlink(missing_ok=True)
```

Also update the two `cmd_reconcile`/`cmd_adopt` call sites in `src/jaunt/cli.py` **only if** they construct `header_fields` or call `load_function_node` directly — search with `grep -n "load_function_node\|reconcile_entry" src/jaunt/cli.py` and route any direct `load_function_node` use through `load_contract_node`.

Note: `_validate_via_pytest` passes `source_roots=[]` because the target import in the battery resolves via the project's own path setup; check `run_battery_file`'s callers in `cli.py` — if reconcile there passes `cfg.paths.source_roots`, thread `source_roots: list[str]` through `reconcile_entry` as a new keyword-only parameter with default `[]` and pass it from the CLI. (Do this now if the fixture test fails with an import error for `mod`.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_contract_reconcile_parity.py tests/test_cli_reconcile.py -q && uv run pytest -q`
Expected: PASS, including the existing no-write-on-failure tests.

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/contract/runner.py src/jaunt/cli.py tests/test_contract_reconcile_parity.py
git commit -m "feat(contract): reconcile over the call-plan IR — async functions + fixture validation split"
```

---

### Task 9: Whole-class reconcile, adopt, eject, and drift matrix

**Files:**
- Modify: `src/jaunt/contract/runner.py` (class branch in `reconcile_entry`)
- Modify: `src/jaunt/cli.py` (`cmd_adopt` ref parsing: reject `module:Cls.method` with the adopt-the-whole-class hint)
- Test: `tests/test_contract_class.py` (create)

**Interfaces:**
- Consumes: everything above.
- Produces — the class branch of `reconcile_entry`:
  1. `async_map = {f"{Cls}.{m.name}": isinstance(m, ast.AsyncFunctionDef) for each method}` plus `{Cls: False}`.
  2. Blocks = the class docstring's blocks (`method=None`) merged with each **public documented** method's blocks parsed with `method=<name>` — `parse_case_blocks(method_doc, target=Cls, async_map=..., module_names=..., method=name)`; merge via `CaseBlocks.merged`.
  3. Regions: cases grouped by `case.method`; group `None` renders with `region_suffix=""`, each method group with `region_suffix=<method>`. Order: class-level first, then methods in source order.
  4. Validation namespace: `{Cls: module_namespace[Cls]}` + import names, as for functions.
  5. Strength: `compute_case_strength(ast.unparse(class_node), Cls, all_blocks, ns)` — mutants of the whole class source.
  6. Battery: `func_name=Cls` (the base import line imports the class), regions as above, same header/`strength-excluded`/fixture-validation flow as Task 8.
  7. A class with no derivable cases anywhere reconciles `ok=True` with an empty-region battery (header only) and a "no derivable cases" note in `ReconcileResult.failures`? **No** — keep `failures=[]`; the note is printed by the CLI when `regions == []` (add `wrote_empty: bool = False` NOT needed; CLI checks `res.ok and not regions` is invisible — instead have the CLI print the note when the written battery has no regions: `if res.ok and "# >>> jaunt:derived" not in res.battery_path.read_text(): print("note: no derivable cases; battery is header-only")`).
- `cmd_adopt`/`cmd_eject` in `cli.py`: when the parsed target contains a dot (`module:Cls.method`), exit with `error: adopt the whole class: jaunt adopt <module>:Cls` (exit 2). Find the current parse at `src/jaunt/cli.py` `cmd_adopt` (search `def cmd_adopt`) — it currently splits on `:`; add the dot check on the qualname part.

- [ ] **Step 1: Write failing tests**

Create `tests/test_contract_class.py`:

```python
"""Whole-class contract mode: reconcile, drift, adopt/eject round-trip."""

from __future__ import annotations

from pathlib import Path

from jaunt.contract.runner import evaluate_entry, reconcile_entry, run_battery_file
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef

CLASS_SRC = '''
class Counter:
    """Counts things.

    Examples:
        - Counter(start=1).peek() == 1
    """

    def __init__(self, start: int = 0) -> None:
        self.n = start

    def peek(self) -> int:
        """Current value."""
        return self.n

    def increment(self, by: int) -> int:
        """Bump and return.

        Examples:
            - Counter().increment(2) == 2

        Raises:
            - Counter().increment(-1) raises ValueError
        """
        if by < 0:
            raise ValueError("negative")
        self.n += by
        return self.n
'''


class Counter:
    def __init__(self, start: int = 0) -> None:
        self.n = start

    def peek(self) -> int:
        return self.n

    def increment(self, by: int) -> int:
        if by < 0:
            raise ValueError("negative")
        self.n += by
        return self.n


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(CLASS_SRC, encoding="utf-8")
    return tmp_path


def _entry(root: Path) -> SpecEntry:
    return SpecEntry(
        kind="contract",
        spec_ref=SpecRef(module="mod", qualname="Counter"),
        module="mod",
        qualname="Counter",
        source_file=str(root / "src" / "mod.py"),
        obj=Counter,
        decorator_kwargs={},
    )


def _reconcile(root: Path, strength: bool = False):
    return reconcile_entry(
        root,
        "tests/contract",
        ["examples", "errors"],
        strength,
        _entry(root),
        module_namespace={"Counter": Counter},
        tool_version="t",
    )


def test_class_reconcile_writes_per_method_regions(tmp_path: Path) -> None:
    root = _project(tmp_path)
    res = _reconcile(root)
    assert res.ok, res.failures
    text = res.battery_path.read_text(encoding="utf-8")
    assert res.battery_path.name == "test_Counter.py"
    assert "from mod import Counter" in text
    assert "# >>> jaunt:derived examples" in text  # class-level block
    assert "# >>> jaunt:derived examples-increment" in text
    assert "# >>> jaunt:derived errors-increment" in text
    assert "assert Counter(start=1).peek() == 1" in text
    assert "assert Counter().increment(2) == 2" in text


def test_class_battery_actually_passes_pytest(tmp_path: Path) -> None:
    root = _project(tmp_path)
    res = _reconcile(root)
    assert run_battery_file(res.battery_path, root=root, source_roots=["src"]) is True


def test_class_reconcile_catches_bad_example(tmp_path: Path) -> None:
    root = _project(tmp_path)
    bad = CLASS_SRC.replace(
        "- Counter().increment(2) == 2", "- Counter().increment(2) == 99"
    )
    (root / "src" / "mod.py").write_text(bad, encoding="utf-8")
    res = _reconcile(root)
    assert res.ok is False
    assert not res.battery_path.exists()


class TestClassDriftMatrix:
    def _evaluated(self, root: Path):
        return evaluate_entry(
            root,
            "tests/contract",
            ["examples", "errors"],
            _entry(root),
            run_battery=lambda p: run_battery_file(p, root=root, source_roots=["src"]),
        )

    def test_in_sync_after_reconcile(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _reconcile(root)
        assert self._evaluated(root).state.value == "in-sync"

    def test_method_docstring_edit_is_stale_prose(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _reconcile(root)
        edited = CLASS_SRC.replace("Bump and return.", "Bump twice and return.")
        (root / "src" / "mod.py").write_text(edited, encoding="utf-8")
        assert self._evaluated(root).state.value == "stale-prose"

    def test_method_resignature_is_signature_drift(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _reconcile(root)
        edited = CLASS_SRC.replace(
            "def peek(self) -> int:", "def peek(self, default: int = 0) -> int:"
        )
        (root / "src" / "mod.py").write_text(edited, encoding="utf-8")
        assert self._evaluated(root).state.value == "signature-drift"

    def test_body_only_edit_is_refactored(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _reconcile(root)
        edited = CLASS_SRC.replace("self.n += by", "self.n = self.n + by")
        (root / "src" / "mod.py").write_text(edited, encoding="utf-8")
        assert self._evaluated(root).state.value == "refactored"


def test_adopt_rejects_method_ref(tmp_path: Path, monkeypatch, capsys) -> None:
    import jaunt.cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n', encoding="utf-8"
    )
    _project(tmp_path)
    code = jaunt.cli.main(["adopt", "mod:Counter.increment", "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "whole class" in err
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contract_class.py -q`
Expected: FAIL — `ValueError: whole-class reconcile not wired yet`.

- [ ] **Step 3: Implement the class branch**

In `reconcile_entry`, replace the Task-8 placeholder `raise` with:

```python
    if isinstance(node, ast.ClassDef):
        return _reconcile_class(
            node,
            root=root,
            battery_dir=battery_dir,
            derive=derive,
            strength_enabled=strength_enabled,
            entry=entry,
            module_namespace=module_namespace,
            tool_version=tool_version,
            path=path,
            spec_ref=spec_ref,
        )
```

and add:

```python
def _reconcile_class(
    node,
    *,
    root: Path,
    battery_dir: str,
    derive: list[str],
    strength_enabled: bool,
    entry: SpecEntry,
    module_namespace: dict[str, object],
    tool_version: str,
    path: Path,
    spec_ref: str,
) -> ReconcileResult:
    import ast

    from jaunt.contract.battery import merge_battery
    from jaunt.contract.cases import CaseBlocks, CaseParseError, parse_case_blocks
    from jaunt.contract.derive import (
        battery_extra_imports,
        derive_case_regions,
        evaluate_cases,
    )
    from jaunt.contract.strength import compute_case_strength, format_strength
    from jaunt.digest import contract_digests

    cls_name = entry.qualname
    methods = [
        m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    async_map: dict[str, bool] = {cls_name: False}
    for m in methods:
        async_map[f"{cls_name}.{m.name}"] = isinstance(m, ast.AsyncFunctionDef)
    module_names = _module_top_level_names(entry.source_file)

    # Partition by which docstring each case came from: class-docstring cases
    # render in the base `examples`/`errors` regions, method-docstring cases in
    # `examples-<method>`/`errors-<method>` regions. (Do NOT partition by
    # `case.method` — a class-docstring case like `Counter(1).peek() == 1` has
    # `method="peek"` for async resolution but still belongs to the class region.)
    try:
        class_doc_blocks = parse_case_blocks(
            ast.get_docstring(node, clean=True) or "",
            target=cls_name,
            async_map=async_map,
            module_names=module_names,
        )
        all_blocks = class_doc_blocks
        method_blocks: list[tuple[str, CaseBlocks]] = []
        for m in methods:
            if m.name.startswith("_"):
                continue
            doc = ast.get_docstring(m, clean=True) or ""
            if not doc:
                continue
            mb = parse_case_blocks(
                doc,
                target=cls_name,
                async_map=async_map,
                module_names=module_names,
                method=m.name,
            )
            if not mb.is_empty():
                method_blocks.append((m.name, mb))
                all_blocks = all_blocks.merged(mb)
    except CaseParseError as exc:
        return ReconcileResult(
            spec_ref, False, "0/0", [f"{exc} (line: {exc.line})"], path, False
        )

    cls_obj = module_namespace.get(cls_name)
    if not callable(cls_obj):
        return ReconcileResult(spec_ref, False, "0/0", ["class not importable"], path, False)

    eval_ns: dict[str, object] = {cls_name: cls_obj}
    for case in (*all_blocks.examples, *all_blocks.raises):
        for name in case.imports:
            eval_ns[name] = module_namespace.get(name)

    failures = evaluate_cases(all_blocks, namespace=eval_ns)
    if failures:
        return ReconcileResult(spec_ref, False, "0/0", failures, path, False)

    digs = contract_digests(entry.source_file, entry.qualname)
    strength = "0/0"
    excluded = 0
    if strength_enabled:
        killed, applicable, excluded = compute_case_strength(
            ast.unparse(node), cls_name, all_blocks, eval_ns
        )
        strength = format_strength(killed, applicable)

    regions = derive_case_regions(class_doc_blocks, target=cls_name, derive=derive)
    for name, mb in method_blocks:
        regions += derive_case_regions(
            mb, target=cls_name, derive=derive, region_suffix=name
        )

    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    text = merge_battery(
        existing,
        import_module=entry.module,
        func_name=cls_name,
        regions=regions,
        header_fields={
            "derived_from": spec_ref,
            "prose_digest": digs.prose,
            "signature": digs.signature,
            "body_digest": digs.body,
            "strength": strength,
            "strength_excluded": str(excluded),
            "tool_version": tool_version,
        },
        extra_imports=battery_extra_imports(all_blocks),
    )

    if all_blocks.has_fixture_cases():
        if not _validate_via_pytest(text, path, root=root):
            return ReconcileResult(
                spec_ref,
                False,
                "0/0",
                ["fixture-dependent cases failed under pytest; run the battery for detail"],
                path,
                False,
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ReconcileResult(spec_ref, True, strength, [], path, True)
```

In `cmd_adopt` (and `cmd_eject`) in `cli.py`, right after the ref is split into module + qualname, add:

```python
    if "." in func_name:
        _eprint(
            f"error: contract mode adopts the whole class: "
            f"jaunt adopt {module_name}:{func_name.split('.')[0]}"
        )
        return EXIT_CONFIG_OR_DISCOVERY
```

(match the actual local variable names at that site).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_contract_class.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/contract/runner.py src/jaunt/cli.py tests/test_contract_class.py
git commit -m "feat(contract): whole-class adopt/reconcile/check/eject with per-method derived regions"
```

---

### Task 10: Check-time fixture failures + strength display + adopt async end-to-end CLI

**Files:**
- Modify: `src/jaunt/cli.py` (strength display with exclusions in `cmd_reconcile`/`cmd_adopt`/`cmd_status` output paths)
- Test: `tests/test_contract_fixtures_e2e.py` (create)

**Interfaces:**
- Produces:
  - CLI human output formats strength as `strength {N}/{M} ({K} fixture cases not scored)` when the battery header has `strength-excluded=K` (K > 0); JSON output gains `"strength_excluded": K` next to existing strength fields. Find the printing sites with `grep -n "strength" src/jaunt/cli.py` and extend each site that prints a `ContractStatus.strength` or `ReconcileResult.strength`.
  - No new behavior in `evaluate_entry` — a missing fixture at check time already fails pytest and lands in `BEHAVIOR_DRIFT`; the test pins it.

- [ ] **Step 1: Write failing tests**

Create `tests/test_contract_fixtures_e2e.py`:

```python
"""End-to-end: adopt an async function via the CLI; fixture failure at check time."""

from __future__ import annotations

import json
from pathlib import Path

import jaunt.cli


def _project(tmp_path: Path, module_src: str) -> None:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n[contract]\nstrength = false\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "amod.py").write_text(module_src, encoding="utf-8")


ASYNC_SRC = '''
async def double(x: int) -> int:
    """Double.

    Examples:
        - double(2) == 4
    """
    return x * 2
'''


def test_adopt_async_function_via_cli(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _project(tmp_path, ASYNC_SRC)

    code = jaunt.cli.main(["adopt", "amod:double", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["ok"] is True

    marked = (tmp_path / "src" / "amod.py").read_text(encoding="utf-8")
    assert "@jaunt.contract" in marked

    battery = tmp_path / "tests" / "contract" / "amod" / "test_double.py"
    text = battery.read_text(encoding="utf-8")
    assert "async def test_examples():" in text

    code = jaunt.cli.main(["check", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out


FIXTURE_SRC = '''
import jaunt


@jaunt.contract
def lookup(db, key: str) -> str:
    """Look up.

    Examples:
        - lookup(db, 'a') == 'A'

    Fixtures: db
    """
    return db[key]
'''


def test_missing_fixture_at_check_is_behavior_drift(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _project(tmp_path, FIXTURE_SRC)
    # Battery written with a working conftest...
    conftest = tmp_path / "tests" / "contract" / "conftest.py"
    conftest.parent.mkdir(parents=True)
    conftest.write_text(
        "import pytest\n\n@pytest.fixture\ndef db():\n    return {'a': 'A'}\n",
        encoding="utf-8",
    )
    code = jaunt.cli.main(["reconcile", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out

    # ...then the conftest disappears: check must block with behavior drift.
    conftest.unlink()
    code = jaunt.cli.main(["check", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 4
    assert any("behavior-drift" in json.dumps(row) for row in out.get("contracts", [out]))
```

(Adapt the JSON shape assertions to the real `cmd_check --json` payload — inspect it with `grep -n "def cmd_check" -A 40 src/jaunt/cli.py` before finalizing the test; the pinned behavior is exit 4 + a behavior-drift state for `amod:lookup`.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contract_fixtures_e2e.py -q`
Expected: `test_adopt_async_function_via_cli` FAILS if any CLI path still rejects async (this is the integration proof for Tasks 4–8); the fixture test may already pass — if both pass, verify by reading the trace that the async battery really contains `async def` (no vacuous pass), then continue.

- [ ] **Step 3: Wire the strength display**

In `cli.py`, wherever reconcile results are printed (search `strength`), extend:

```python
    excluded = int(header.get("strength-excluded", "0")) if header else 0
    suffix = f" ({excluded} fixture cases not scored)" if excluded else ""
    print(f"- {ref}: strength {strength}{suffix}")
```

(match each site's existing format; JSON payloads get `"strength_excluded": excluded`). For `cmd_status`'s contract rows, read the parsed battery header via the existing `parse_battery` call chain — `evaluate_entry` already returns `strength` from the header; add `strength_excluded` to `ContractStatus` (new field, default `0`) populated from `header.get("strength-excluded", "0")` in `evaluate_entry`, and emit it in both output modes.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_contract_fixtures_e2e.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/cli.py src/jaunt/contract/runner.py tests/test_contract_fixtures_e2e.py
git commit -m "feat(contract): CLI async adopt e2e, strength exclusion display, check-time fixture drift pinned"
```

---

### Task 11: Docs, dogfood, and cleanup

**Files:**
- Modify: `CLAUDE.md` (contract-mode bullet: async + classes + fixtures), `README.md` (Two Modes section: one sentence on parity), `docs/hooks.md` only if it mentions sync-only contract mode (grep first)
- Modify: `src/jaunt/instructions/primer.md` if it describes contract-mode limits (grep `contract` there)
- Delete: legacy-only code paths that are now dead — **check first**: if `extract_blocks_structured`/`derive_regions`/`evaluate_blocks`/`compute_strength` (legacy) have no remaining callers outside tests after Task 8, delete them and migrate their byte-compat assertions into `tests/test_contract_cases.py` as golden-string tests (the byte-compat tests from Task 2 must keep the golden *strings*, not call the deleted functions — inline today's expected region text as literals)
- Test: dogfood — adopt an async function in jaunt's own repo

**Steps:**

- [ ] **Step 1: Grep for stale docs and dead code**

Run:
```bash
grep -rn "top-level sync\|sync functions only\|v1" CLAUDE.md README.md docs/ src/jaunt/instructions/ src/jaunt/contract/ src/jaunt/runtime.py | grep -i contract
grep -rn "extract_blocks_structured\|derive_regions\|evaluate_blocks(" src/jaunt/ | grep -v cases.py
```
Update every hit: docs now say contract mode covers top-level functions (sync or async) and whole classes, with `Fixtures:` case support resolved from `tests/contract/conftest.py`. If the legacy derive functions have no production callers, delete them plus `ExampleRow`/`RaisesRow`/`ContractBlocks` **unless** `extract_blocks_via_model` still returns `ContractBlocks` (it does — keep `ContractBlocks`/`ExampleRow`/`RaisesRow` and the model path, delete only unreferenced renderers, and keep `_render_examples_region`/`_render_errors_region` since Task 2's legacy path calls them).

- [ ] **Step 2: Update CLAUDE.md key-concepts bullet**

In the `## Key Concepts` contract-mode bullet, replace the coverage sentence with: "Covers top-level functions (sync or async) and whole classes; derived cases may declare pytest fixtures (`Fixtures: db`) resolved from `tests/contract/conftest.py`."

- [ ] **Step 3: Dogfood — adopt an async function in jaunt itself**

Pick `src/jaunt/journal.py`'s smallest pure sync function OR any small async helper; if no clean async candidate exists, add the parity example instead: create `examples/contract_async/` mirroring `examples/contract_slugify/`'s layout (jaunt.toml + src module with one async function using call-form Examples + README) and run:

```bash
cd examples/contract_async && uv run --project ../.. jaunt adopt amod:fetch_slug && uv run --project ../.. jaunt check
```
Expected: adopt exit 0, battery under `examples/contract_async/tests/contract/`, check exit 0. Commit the example including its battery.

- [ ] **Step 4: Full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs(contract): adoption-parity docs + async contract example; drop dead legacy derive paths"
```

---

## Self-Review Checklist (run after Task 11)

1. **Spec coverage:** case grammar (§1 → Tasks 1–3), async (§2 → Tasks 4,5,7,8,10), classes (§3 → Tasks 4,5,6,9), fixture seam (§4 → Tasks 3,8,10), error handling (§5 → parse-error tests in Tasks 1,8; behavior-drift in Task 10), out-of-scope respected (no method adoption — rejection tested in Task 9).
2. **Byte-compat:** Task 2's `test_all_legacy_renders_byte_identical_to_today` and Task 4's golden digest tests both still pass after Task 11's deletions.
3. **Invariants:** every reconcile failure path returns `wrote=False` and leaves no file (Tasks 8, 9); `_jaunt_validate_*` never survives (Task 8 test).
4. Run the drift matrix (Task 9) one final time against the finished code: `uv run pytest tests/test_contract_class.py -q`.
