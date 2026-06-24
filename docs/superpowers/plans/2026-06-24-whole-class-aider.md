> **SUPERSEDED by `docs/superpowers/specs/2026-06-24-codex-engine-design.md`** — Codex's whole-file output delivers whole-class @magic natively; the aider escalation/fallback approach here was not implemented. Retained for its Codex-review history.

# Whole-class `@jaunt.magic` under the default aider engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make whole-class `@jaunt.magic` generation (docstring-only / stubs / mix) succeed under the **default `aider` engine** by seeding aider a deterministic scaffold + contract, validating the class shape *inside* the retry loop, and falling back to the direct backend as a last resort.

**Architecture:** Three deterministic, pure-function layers feed one orchestration layer. (1) `class_analysis.py` turns a spec's class source into an aider *seed scaffold* (real anchors aider can diff against) and a *whole-class contract* block. (2) `validation.py` gains three class-shape guards (unfilled-stub via AST, docstring-only completeness, attribute preservation). (3) `ModuleSpecContext` carries the seed + contract + a `whole_class` flag, which the aider backend uses to seed the first attempt and to escalate diff→whole-file on contract failure. (4) `builder.py` wires the scaffold/contract per *component*, runs the class validator *in-loop* (so escalation actually fires), and retries failed whole-class modules once with an injected `fallback_backend`. `cli.py` constructs that fallback from `cfg.llm.provider`.

**Tech Stack:** Python 3.12, `ast`, pytest, `uv`. LLM backends: aider (default) + direct OpenAI/Anthropic/Cerebras (`GeneratorBackend`). No new dependencies.

## Global Constraints

- Python 3.12+; ruff line-length 100, rules E/F/I/UP/B; `uv run ruff check .` and `uv run ty check` must pass.
- No behavior change for function-only and per-method-class modules — scaffold-seeding is **strictly gated** on components whose expected output contains a whole-class `@magic` spec.
- Do not change the direct/legacy backend's generation behavior.
- The "intensive" lever is **context** (scaffold + contract), not extra compute — do not bump reasoning effort.
- Preserved methods (heuristic-real + `@jaunt.preserve`) must survive verbatim; the existing class-validator guarantees still hold.
- Run the full suite (`uv run pytest`) after each task; it uses mocks and needs no API key.
- A whole-class `@magic` spec is identified exactly as `builder._whole_class_specs` does: `entry.class_name is None and "." not in entry.qualname and isinstance(entry.obj, type)`.
- The `# jaunt:implement` sentinel comment is the literal string `# jaunt:implement`. The stub body message is `jaunt: implement <Class>.<method> per the spec`.

---

## File Structure

- `src/jaunt/class_analysis.py` — **(modify)** add `collect_spec_module_imports`, `build_class_scaffold`, `render_whole_class_contract` (+ private helpers). Pure functions over class source segments; no `jaunt` imports beyond `ast`/stdlib.
- `src/jaunt/validation.py` — **(modify)** extend `validate_build_class_source` with three guards + two new keyword params (defaulted, back-compatible); add `_class_attribute_nodes`.
- `src/jaunt/generate/base.py` — **(modify)** add `seed_target_content`, `whole_class_contract_block`, `whole_class` fields to `ModuleSpecContext`.
- `src/jaunt/cache.py` — **(modify)** include the three new ctx fields in `cache_key_from_context`.
- `src/jaunt/generate/aider_backend.py` — **(modify)** seed `target_content` from `ctx.seed_target_content`; write the contract as a `context/` read-only file; escalate diff→whole-file on contract failure when `ctx.whole_class`.
- `src/jaunt/builder.py` — **(modify)** per-component scaffold/contract into ctx; class-aware in-loop retry validator; populate new `_class_validation_inputs` keys; `run_build(..., fallback_backend=None)` + fallback orchestration; add module logger.
- `src/jaunt/cli.py` — **(modify)** split out `_build_direct_backend`/`_build_fallback_backend`; pass `fallback_backend` into `run_build` and `RepairBuildContext`.
- `src/jaunt/tester.py` — **(modify)** add `fallback_backend` to `RepairBuildContext`; forward to its `run_build` call.
- `examples/06_whole_class/jaunt.toml`, `README.md` — **(modify)** drop the `[agent] engine = "legacy"` pin and its note.
- Memory `aider-whole-class-gap.md` + `MEMORY.md` — **(modify)** mark the gap closed.
- Tests: `tests/test_class_scaffold.py` **(create)**; extend `tests/test_validation_class.py`, `tests/test_cache.py` (or create if absent), `tests/test_aider_backend.py`, `tests/test_builder_whole_class.py`.

---

## Task 1: Validation guards (AST unfilled-stub, docstring-only completeness, attribute preservation)

**Files:**
- Modify: `src/jaunt/validation.py` (`validate_build_class_source` ~429-494; add `_class_attribute_nodes`)
- Test: `tests/test_validation_class.py`

**Interfaces:**
- Consumes: `jaunt.class_analysis.is_stub_body` (existing).
- Produces: `validate_build_class_source(..., class_attributes: dict[str, str] | None = None, require_public_method: bool = False) -> list[str]` (`class_attributes` maps attr name → normalized source). New params are **defaulted** so the existing `_class_validation_inputs` caller (which passes `**kw`) keeps working until Task 5 supplies them.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_validation_class.py`:

```python
def test_fails_when_stub_left_unfilled_even_if_sentinel_stripped() -> None:
    # Aider can replace a body with a bare ``raise NotImplementedError`` and drop the
    # sentinel comment — a text check would pass, so detection must be AST-based.
    src = (
        'class C:\n    "A class."\n'
        "    def do(self):\n        raise NotImplementedError\n"
    )
    errs = validate_build_class_source(src, **BASE_KW)
    assert any("stub" in e for e in errs)


def test_passes_when_stub_filled() -> None:
    src = 'class C:\n    "A class."\n    def do(self):\n        return 1\n'
    assert validate_build_class_source(src, **BASE_KW) == []


def test_docstring_only_empty_class_fails() -> None:
    src = 'class C:\n    "A class."\n    pass\n'
    kw = _kw(stub_methods=[], require_public_method=True)
    errs = validate_build_class_source(src, **kw)
    assert any("public method" in e for e in errs)


def test_docstring_only_with_public_method_passes() -> None:
    src = 'class C:\n    "A class."\n    def total(self):\n        return 0\n'
    kw = _kw(stub_methods=[], require_public_method=True)
    assert validate_build_class_source(src, **kw) == []


def test_fails_when_class_attribute_dropped() -> None:
    src = 'class C:\n    "A class."\n    def do(self):\n        return 1\n'
    kw = _kw(class_attributes={"CAPACITY": "CAPACITY: int = 10"})
    errs = validate_build_class_source(src, **kw)
    assert any("CAPACITY" in e for e in errs)


def test_fails_when_class_attribute_value_changed() -> None:
    # name-only checking would accept this; the annotation/value must match too.
    src = (
        'class C:\n    "A class."\n    CAPACITY = None\n'
        "    def do(self):\n        return 1\n"
    )
    kw = _kw(class_attributes={"CAPACITY": "CAPACITY: int = 10"})
    errs = validate_build_class_source(src, **kw)
    assert any("CAPACITY" in e for e in errs)


def test_passes_when_class_attribute_retained() -> None:
    src = (
        'class C:\n    "A class."\n    CAPACITY: int = 10\n'
        "    def do(self):\n        return 1\n"
    )
    kw = _kw(class_attributes={"CAPACITY": "CAPACITY: int = 10"})
    assert validate_build_class_source(src, **kw) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_validation_class.py -q`
Expected: FAIL — `validate_build_class_source() got an unexpected keyword argument 'require_public_method'` (and the unfilled-stub test fails because no such check exists yet).

- [ ] **Step 3: Implement the guards**

In `src/jaunt/validation.py`, add the top-level import near the other imports:

```python
from jaunt.class_analysis import is_stub_body
```

Add this helper next to `_method_nodes` (it maps each class-attribute name to the
**normalized source** of its declaration, so the guard checks annotation + value, not
just the name — Codex finding #5):

```python
def _class_attribute_nodes(cls: ast.ClassDef) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in cls.body:
        if isinstance(node, ast.Assign):
            rendered = ast.unparse(node)
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = rendered
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = ast.unparse(node)
    return out
```

Change the `validate_build_class_source` signature to add the two params. `class_attributes`
maps attr name → the normalized source (`ast.unparse(node)`) of the spec's declaration:

```python
def validate_build_class_source(
    source: str,
    *,
    class_name: str,
    stub_methods: list[str],
    preserved_segments: dict[str, str],
    declared_bases: list[str],
    class_decorators: list[str],
    required_abstractmethods: list[str],
    spec_docstring: str,
    class_attributes: dict[str, str] | None = None,
    require_public_method: bool = False,
) -> list[str]:
```

Then, inside the function, **after** the existing docstring-retention block and **before** `return errors`, insert:

```python
    # Unfilled-stub detection (AST, not the sentinel comment): each declared stub
    # method must have a real body in the output.
    for name in stub_methods:
        node = methods.get(name)
        if node is not None and is_stub_body(node):
            errors.append(
                f"{class_name}: method {name!r} was left as a stub; implement it per the spec."
            )

    # Class-attribute preservation: every spec class attribute must survive with the
    # same annotation/value (compared modulo formatting via ast.unparse round-trip).
    if class_attributes:
        actual_attrs = _class_attribute_nodes(cls)
        for attr_name, expected_src in class_attributes.items():
            actual_src = actual_attrs.get(attr_name)
            if actual_src is None:
                errors.append(
                    f"{class_name}: class attribute {attr_name!r} from the spec was not preserved."
                )
            elif actual_src != expected_src:
                errors.append(
                    f"{class_name}: class attribute {attr_name!r} was modified; "
                    "keep it exactly as declared in the spec."
                )

    # Docstring-only completeness: a docstring-only spec must yield a non-trivial class.
    if require_public_method and not any(not name.startswith("_") for name in methods):
        errors.append(
            f"{class_name}: docstring-only spec must define at least one public method."
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_validation_class.py -q`
Expected: PASS (all existing + new tests).

- [ ] **Step 5: Lint, typecheck, full suite**

Run: `uv run ruff check src/jaunt/validation.py tests/test_validation_class.py && uv run ty check && uv run pytest -q`
Expected: clean; all pass.

- [ ] **Step 6: Commit**

```bash
git add src/jaunt/validation.py tests/test_validation_class.py
git commit -m "feat(validation): add unfilled-stub, docstring-only, and attribute guards for whole-class builds"
```

---

## Task 2: Scaffold builder, import collector, and contract renderer

**Files:**
- Modify: `src/jaunt/class_analysis.py` (add three public functions + private helpers)
- Test: `tests/test_class_scaffold.py` (create)

**Interfaces:**
- Consumes: existing `split_class_members`, `classify_class_mode`, `is_preserve_decorator`, `_iter_methods`.
- Produces:
  - `collect_spec_module_imports(spec_source: str) -> list[str]` — every top-level `import`/`from … import`, unparsed, in source order.
  - `build_class_scaffold(class_segment: str) -> str` — scaffold for ONE whole-class spec: header (bases + decorators, `@magic` stripped), docstring, class attributes verbatim, preserved methods (`@jaunt.preserve` stripped), stub methods → signature + docstring + `raise NotImplementedError("jaunt: implement <Class>.<method> per the spec")  # jaunt:implement`. Docstring-only classes (no methods) get `pass`.
  - `render_whole_class_contract(*, class_segment: str, base_contract_block: str) -> str` — the per-component contract prose.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_class_scaffold.py`:

```python
from __future__ import annotations

import ast

from jaunt.class_analysis import (
    build_class_scaffold,
    collect_spec_module_imports,
    render_whole_class_contract,
)

STUB_CLASS = (
    "@jaunt.magic()\n"
    "class Stack(Base):\n"
    '    """A stack. LIFO."""\n'
    "    CAPACITY: int = 10\n"
    "    def push(self, x: int) -> None:\n"
    '        """Push x."""\n'
    "        ...\n"
    "    @jaunt.preserve\n"
    "    def is_empty(self) -> bool:\n"
    "        return self._n == 0\n"
)

DOCSTRING_ONLY = '@jaunt.magic()\nclass Inv:\n    """An inventory. add/remove/total."""\n'


def test_collect_imports_includes_all_top_level_imports() -> None:
    src = (
        "import os\n"
        "from typing import Any\n"
        "import jaunt\n\n"
        "@jaunt.magic()\n"
        "class C:\n"
        "    import sys  # not top-level\n"
        "    def f(self): ...\n"
    )
    imports = collect_spec_module_imports(src)
    assert "import os" in imports
    assert "from typing import Any" in imports
    assert "import jaunt" in imports
    assert all("sys" not in imp for imp in imports)


def test_scaffold_renders_header_attrs_docstring_preserved_and_sentinel_stub() -> None:
    scaffold = build_class_scaffold(STUB_CLASS)
    tree = ast.parse(scaffold)  # must be valid Python
    cls = tree.body[0]
    assert isinstance(cls, ast.ClassDef)
    # base + class attribute + docstring retained
    assert "Base" in {ast.unparse(b) for b in cls.bases}
    assert "CAPACITY" in scaffold and "= 10" in scaffold
    assert "A stack. LIFO." in scaffold
    # @magic stripped from the header
    assert "@jaunt.magic" not in scaffold
    # preserved method body kept, @jaunt.preserve stripped
    assert "self._n == 0" in scaffold
    assert "@jaunt.preserve" not in scaffold
    # stub becomes a sentinel body
    assert "# jaunt:implement" in scaffold
    assert "jaunt: implement Stack.push per the spec" in scaffold


def test_scaffold_docstring_only_is_header_docstring_pass() -> None:
    scaffold = build_class_scaffold(DOCSTRING_ONLY)
    ast.parse(scaffold)
    assert "An inventory" in scaffold
    assert scaffold.rstrip().endswith("pass")


def test_contract_lists_fill_preserve_and_docstring_only_directive() -> None:
    c1 = render_whole_class_contract(class_segment=STUB_CLASS, base_contract_block="(no base classes)")
    assert "Stack.push" in c1
    assert "Stack.is_empty" in c1
    assert "jaunt:implement" in c1
    c2 = render_whole_class_contract(class_segment=DOCSTRING_ONLY, base_contract_block="(no base classes)")
    assert "public method" in c2.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_class_scaffold.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_class_scaffold'`.

- [ ] **Step 3: Implement the functions**

Append to `src/jaunt/class_analysis.py`:

```python
_IMPLEMENT_SENTINEL = "# jaunt:implement"


def _is_magic_decorator(dec: ast.expr) -> bool:
    """True for ``@jaunt.magic``/``@magic`` and their called forms (local copy to keep
    this module dependency-free)."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id == "jaunt"
            and target.attr == "magic"
        )
    if isinstance(target, ast.Name):
        return target.id == "magic"
    return False


def collect_spec_module_imports(spec_source: str) -> list[str]:
    """Every top-level import / from-import in the spec module, unparsed, in order.

    Unlike preamble extraction this does not stop at the first decorated def, so an
    import that only a preserved method or class decorator needs is not dropped.
    """
    try:
        mod = ast.parse(spec_source or "")
    except SyntaxError:
        return []
    out: list[str] = []
    for node in mod.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            rendered = ast.unparse(node).strip()
            if rendered:
                out.append(rendered)
    return out


def _stub_node_with_sentinel(
    node: ast.FunctionDef | ast.AsyncFunctionDef, class_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    clone = ast.parse(ast.unparse(node)).body[0]
    assert isinstance(clone, (ast.FunctionDef, ast.AsyncFunctionDef))
    body: list[ast.stmt] = []
    doc = ast.get_docstring(node, clean=False)
    if doc is not None:
        body.append(ast.Expr(value=ast.Constant(value=doc)))
    msg = f"jaunt: implement {class_name}.{node.name} per the spec"
    body.append(ast.parse(f"raise NotImplementedError({msg!r})").body[0])
    clone.body = body
    return clone


def _attach_sentinels(text: str) -> str:
    """Re-attach the ``# jaunt:implement`` comment that ``ast.unparse`` drops."""
    out: list[str] = []
    for line in text.splitlines():
        if (
            "raise NotImplementedError" in line
            and "jaunt: implement" in line
            and _IMPLEMENT_SENTINEL not in line
        ):
            line = f"{line}  {_IMPLEMENT_SENTINEL}"
        out.append(line)
    return "\n".join(out)


def build_class_scaffold(class_segment: str) -> str:
    """Aider seed scaffold for a single whole-class @magic spec (see module docstring)."""
    cls = ast.parse(class_segment).body[0]
    assert isinstance(cls, ast.ClassDef)
    class_name = cls.name

    new_body: list[ast.stmt] = []
    doc = ast.get_docstring(cls, clean=False)
    if doc is not None:
        new_body.append(ast.Expr(value=ast.Constant(value=doc)))
    for node in cls.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            new_body.append(node)

    split = split_class_members(cls)
    methods = {n.name: n for n in _iter_methods(cls)}

    for name in split.preserved:
        clone = ast.parse(ast.unparse(methods[name])).body[0]
        assert isinstance(clone, (ast.FunctionDef, ast.AsyncFunctionDef))
        clone.decorator_list = [
            d for d in clone.decorator_list if not is_preserve_decorator(d)
        ]
        new_body.append(clone)

    for name in split.stubs:
        new_body.append(_stub_node_with_sentinel(methods[name], class_name))

    # Emit `pass` only when the class declares no methods (docstring-only / attrs-only),
    # regardless of a docstring already being present (Codex finding #1).
    if not split.stubs and not split.preserved:
        new_body.append(ast.Pass())

    new_cls = ast.ClassDef(
        name=class_name,
        bases=cls.bases,
        keywords=cls.keywords,
        body=new_body,
        decorator_list=[d for d in cls.decorator_list if not _is_magic_decorator(d)],
        type_params=getattr(cls, "type_params", []),
    )
    ast.fix_missing_locations(new_cls)
    return _attach_sentinels(ast.unparse(new_cls)).rstrip() + "\n"


def render_whole_class_contract(*, class_segment: str, base_contract_block: str) -> str:
    cls = ast.parse(class_segment).body[0]
    assert isinstance(cls, ast.ClassDef)
    split = split_class_members(cls)
    mode = classify_class_mode(cls)

    lines = [f"# Whole-class generation contract: {cls.name}", ""]
    if split.stubs:
        lines.append(
            "Replace each `# jaunt:implement` method body with a real implementation "
            "(remove the sentinel and the NotImplementedError):"
        )
        lines.extend(f"- {cls.name}.{name}" for name in split.stubs)
        lines.append("")
    if split.preserved:
        lines.append("Keep these methods EXACTLY as written — do not modify their bodies:")
        lines.extend(f"- {cls.name}.{name}" for name in split.preserved)
        lines.append("")
    if mode == "docstring_only":
        lines.append(
            "Design the full public API the class docstring implies; define real public "
            "methods (an empty class body is invalid)."
        )
        lines.append("")
    block = base_contract_block.strip()
    if block and block != "(no base classes)":
        lines.append(
            "Base-class / abstractmethod contract — implement all inherited "
            "abstractmethods and keep overrides signature-compatible:"
        )
        lines.append(block)
        lines.append("")
    lines.extend(
        [
            "Retain the class docstring (you may add to it).",
            "Preserve declared base classes, class decorators, and class attributes verbatim.",
            "You may add `__init__`, private helpers, and shared state as needed.",
        ]
    )
    return "\n".join(lines) + "\n"
```

> **Note (no comment marker — Codex #2/#3):** an earlier draft injected a `# preserved`
> comment via a string-expr placeholder + global `.replace()`. That was dropped: it broke
> when the marker became the class docstring (no preceding docstring/attrs) and could
> corrupt a legitimate `'preserved — do not modify'` string literal. Aider already learns
> which methods to leave alone from the contract block (Task 2 renderer lists them
> explicitly), so the in-scaffold comment is unnecessary.
>
> **Note (preserved-method normalization — Codex #4):** preserved methods are round-tripped
> through `ast.unparse` in the seed, so their comments/formatting may be normalized. This is
> intentional and consistent with the shipped preserved-method contract, which is enforced
> by **AST-equivalence** (`validate_build_class_source` compares decorators-stripped ASTs;
> see `tests/test_validation_class.py::test_passes_when_preserved_method_intact_modulo_formatting`
> and the `ast.unparse`-based `_class_validation_inputs`). The source spec remains canonical;
> only the generated copy is normalized. Bodies (the behavior `@jaunt.preserve` protects) are
> preserved exactly.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_class_scaffold.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck, full suite**

Run: `uv run ruff check src/jaunt/class_analysis.py tests/test_class_scaffold.py && uv run ty check && uv run pytest -q`
Expected: clean; all pass.

- [ ] **Step 6: Commit**

```bash
git add src/jaunt/class_analysis.py tests/test_class_scaffold.py
git commit -m "feat(class_analysis): add scaffold builder, import collector, and whole-class contract renderer"
```

---

## Task 3: `ModuleSpecContext` fields + cache key

**Files:**
- Modify: `src/jaunt/generate/base.py` (`ModuleSpecContext` ~16-35)
- Modify: `src/jaunt/cache.py` (`cache_key_from_context` ~33-101)
- Test: `tests/test_cache.py` (extend; create if absent)

**Interfaces:**
- Produces: `ModuleSpecContext.seed_target_content: str = ""`, `.whole_class_contract_block: str = ""`, `.whole_class: bool = False`. `cache_key_from_context` now mixes all three into the digest.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cache.py` (create the file with this content if it does not exist):

```python
from __future__ import annotations

from jaunt.cache import cache_key_from_context
from jaunt.generate.base import ModuleSpecContext


def _ctx(**overrides) -> ModuleSpecContext:
    base = dict(
        kind="build",
        spec_module="pkg.mod",
        generated_module="pkg.__generated__.mod",
        expected_names=["C"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )
    base.update(overrides)
    return ModuleSpecContext(**base)


def test_cache_key_changes_with_seed_target_content() -> None:
    a = cache_key_from_context(_ctx(), model="m", provider="p")
    b = cache_key_from_context(_ctx(seed_target_content="class C: ..."), model="m", provider="p")
    assert a != b


def test_cache_key_changes_with_whole_class_flag_and_contract() -> None:
    base = cache_key_from_context(_ctx(), model="m", provider="p")
    flagged = cache_key_from_context(_ctx(whole_class=True), model="m", provider="p")
    contracted = cache_key_from_context(
        _ctx(whole_class_contract_block="fill push"), model="m", provider="p"
    )
    assert len({base, flagged, contracted}) == 3
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cache.py -q`
Expected: FAIL — `TypeError: ModuleSpecContext.__init__() got an unexpected keyword argument 'seed_target_content'`.

- [ ] **Step 3: Add the context fields**

In `src/jaunt/generate/base.py`, add to `ModuleSpecContext` (after `async_runner`):

```python
    seed_target_content: str = ""
    whole_class_contract_block: str = ""
    whole_class: bool = False
```

- [ ] **Step 4: Mix the fields into the cache key**

In `src/jaunt/cache.py`, in `cache_key_from_context`, immediately **before** the final `module_context_digest` block (the `h.update((ctx.module_context_digest or "").encode())` line), insert:

```python
    h.update((ctx.seed_target_content or "").encode())
    h.update(b"\x00")
    h.update((ctx.whole_class_contract_block or "").encode())
    h.update(b"\x00")
    h.update(b"1" if ctx.whole_class else b"0")
    h.update(b"\x00")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_cache.py -q`
Expected: PASS.

- [ ] **Step 6: Lint, typecheck, full suite**

Run: `uv run ruff check src/jaunt/generate/base.py src/jaunt/cache.py tests/test_cache.py && uv run ty check && uv run pytest -q`
Expected: clean; all pass.

- [ ] **Step 7: Commit**

```bash
git add src/jaunt/generate/base.py src/jaunt/cache.py tests/test_cache.py
git commit -m "feat(context): carry scaffold seed + whole-class contract on ModuleSpecContext and cache key"
```

---

## Task 4: Aider backend — seed the scaffold, surface the contract, escalate to whole-file

**Files:**
- Modify: `src/jaunt/generate/aider_backend.py` (`_plan_attempt` ~237-298; `_make_task` ~310-426)
- Test: `tests/test_aider_backend.py`

**Interfaces:**
- Consumes: `ModuleSpecContext.seed_target_content`, `.whole_class_contract_block`, `.whole_class` (Task 3).
- Produces: first attempt (`failure_kind is None`) seeds `attempt_plan.target_content = ctx.seed_target_content`; a `context/whole_class_contract.md` read-only file when the contract block is non-empty; for `ctx.whole_class`, a contract/structural failure escalates the editor to whole-file (`editor-whole` in architect mode, `whole` in code mode).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_aider_backend.py`:

```python
def _whole_class_ctx(**overrides) -> ModuleSpecContext:
    base = dict(
        kind="build",
        spec_module="pkg.mod",
        generated_module="pkg.__generated__.mod",
        expected_names=["Stack"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
        seed_target_content="class Stack:\n    def push(self): ...\n",
        whole_class_contract_block="# contract\nfill Stack.push\n",
        whole_class=True,
    )
    base.update(overrides)
    return ModuleSpecContext(**base)


def _backend() -> AiderGeneratorBackend:
    llm = LLMConfig(provider="anthropic", model="claude-sonnet-4-6", api_key_env="ANTHROPIC_API_KEY")
    prompts = PromptsConfig(build_system="", build_module="", test_system="", test_module="")
    return AiderGeneratorBackend(llm, AiderConfig(), prompts)


def test_first_attempt_seeds_scaffold_content() -> None:
    be = _backend()
    plan = be._plan_attempt(ctx=_whole_class_ctx(), previous_source="", failure_kind=None)
    assert "class Stack" in plan.target_content
    assert plan.editor_edit_format == "editor-diff"  # architect default


def test_contract_failure_escalates_to_whole_file_for_whole_class() -> None:
    be = _backend()
    plan = be._plan_attempt(
        ctx=_whole_class_ctx(), previous_source="class Stack: ...", failure_kind="contract"
    )
    assert plan.editor_edit_format == "editor-whole"


def test_non_whole_class_contract_failure_keeps_diff() -> None:
    be = _backend()
    ctx = _whole_class_ctx(whole_class=False, seed_target_content="")
    plan = be._plan_attempt(ctx=ctx, previous_source="x = 1", failure_kind="contract")
    assert plan.editor_edit_format == "editor-diff"


def test_make_task_includes_whole_class_contract_file() -> None:
    be = _backend()
    plan = be._plan_attempt(ctx=_whole_class_ctx(), previous_source="", failure_kind=None)
    task = be._make_task(_whole_class_ctx(), attempt_plan=plan, extra_error_context=None)
    paths = {f.relative_path for f in task.read_only_files}
    assert "context/whole_class_contract.md" in paths
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_aider_backend.py -q -k "whole_class or seeds_scaffold or escalates or contract_file"`
Expected: FAIL — the first attempt seeds `""`, no whole-file escalation on `"contract"`, no contract file.

- [ ] **Step 3: Seed the scaffold on the first attempt**

In `src/jaunt/generate/aider_backend.py`, `_plan_attempt`, replace the `failure_kind is None` branch's two `target_content=""` with `target_content=ctx.seed_target_content`:

```python
        if failure_kind is None:
            if configured_mode == "architect":
                return _AttemptPlan(
                    mode="architect",
                    edit_format="architect",
                    editor_edit_format="editor-diff",
                    target_content=ctx.seed_target_content,
                    retry_strategy=None,
                    editor_reasoning_effort="low",
                )
            return _AttemptPlan(
                mode="code",
                edit_format="diff",
                editor_edit_format=None,
                target_content=ctx.seed_target_content,
                retry_strategy=None,
            )
```

- [ ] **Step 4: Escalate to whole-file on whole-class contract failure**

Still in `_plan_attempt`, replace the final two `return _AttemptPlan(...)` branches (the architect `structural_repair`/editor-diff branch and the trailing code/`whole` branch) with:

```python
        if configured_mode == "architect":
            return _AttemptPlan(
                mode="architect",
                edit_format="architect",
                editor_edit_format="editor-whole" if ctx.whole_class else "editor-diff",
                target_content=previous_source or ctx.seed_target_content,
                retry_strategy="structural_repair",
                editor_reasoning_effort="low",
            )

        return _AttemptPlan(
            mode="code",
            edit_format="whole",
            editor_edit_format=None,
            target_content=previous_source or ctx.seed_target_content,
            retry_strategy="structural_repair",
        )
```

> The existing `edit_apply` (architect) and `typecheck`/`narrow_contract` branches are
> unchanged; this only changes the generic structural-repair fallback, which is where a
> whole-class "missing class / unfilled stub" failure lands.

- [ ] **Step 5: Surface the contract as a read-only file**

In `_make_task`, after the `package_context_block` read-only block (~line 367-373), add:

```python
        if (ctx.whole_class_contract_block or "").strip():
            read_only_files.append(
                AgentFile(
                    relative_path="context/whole_class_contract.md",
                    content=ctx.whole_class_contract_block.rstrip() + "\n",
                )
            )
```

And in the `instruction_lines` assembly (after the `package_context_block` instruction ~398-401), add:

```python
        if (ctx.whole_class_contract_block or "").strip():
            instruction_lines.append(
                "Read `context/whole_class_contract.md`: implement every `# jaunt:implement` "
                "method and keep preserved methods verbatim."
            )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_aider_backend.py -q`
Expected: PASS (new + existing).

- [ ] **Step 7: Lint, typecheck, full suite**

Run: `uv run ruff check src/jaunt/generate/aider_backend.py tests/test_aider_backend.py && uv run ty check && uv run pytest -q`
Expected: clean; all pass.

- [ ] **Step 8: Commit**

```bash
git add src/jaunt/generate/aider_backend.py tests/test_aider_backend.py
git commit -m "feat(aider): seed whole-class scaffold, surface contract file, escalate to whole-file on contract failure"
```

---

## Task 5: Builder wiring — per-component scaffold/contract + class-aware in-loop validator

**Files:**
- Modify: `src/jaunt/builder.py` (`_class_validation_inputs` ~288-321; `_component_payload` ~1209-1276; `_make_validators` ~1278-1316)
- Test: `tests/test_builder_whole_class.py`

**Interfaces:**
- Consumes: `class_analysis.build_class_scaffold`, `collect_spec_module_imports`, `render_whole_class_contract`, `resolve_base_contract` (Task 2); `validate_build_class_source(..., class_attributes=, require_public_method=)` (Task 1).
- Produces: for components containing a whole-class spec, `ModuleSpecContext` carries `seed_target_content`, `whole_class_contract_block`, `whole_class=True`; the component's **retry validator** runs `validate_build_class_source` (so the aider retry loop sees missing-method / unfilled-stub failures and escalates). Function-only components get `whole_class=False` and empty seed.

- [ ] **Step 1: Write the failing tests**

`tests/test_builder_whole_class.py` already has `_StubBackend`, `_write_spec`, `_entry`. Add a backend that records the ctx it receives and a validator-exercising test:

```python
class _RecordingBackend(GeneratorBackend):
    """Captures the ctx passed to generate_with_retry and returns a fixed source."""

    def __init__(self, source: str) -> None:
        self._source = source
        self.seen_ctx: ModuleSpecContext | None = None

    @property
    def model_name(self) -> str:
        return "rec"

    @property
    def provider_name(self) -> str:
        return "rec"

    async def generate_module(self, ctx, *, extra_error_context=None):
        return self._source, None

    async def generate_with_retry(
        self, ctx, *, max_attempts=2, extra_validator=None,
        initial_error_context=None, progress=None,
    ):
        from jaunt.generate.base import GenerationResult
        from jaunt.validation import validate_generated_source

        self.seen_ctx = ctx
        errs = validate_generated_source(self._source, ctx.expected_names)
        if not errs and extra_validator is not None:
            errs = extra_validator(self._source)
        return GenerationResult(attempts=1, source=self._source, errors=errs, usage=None)


def _run_build_with(tmp_path: Path, backend: GeneratorBackend) -> BuildReport:
    spec_path = _write_spec(tmp_path)
    entry = _entry(spec_path)
    specs = {entry.spec_ref: entry}
    module_specs = {"pkg.mod": [entry]}
    spec_graph = build_spec_graph(specs, infer_default=False)
    return asyncio.run(
        run_build(
            package_dir=tmp_path / "src",
            generated_dir="__generated__",
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            module_dag={"pkg.mod": set()},
            stale_modules={"pkg.mod"},
            backend=backend,
        )
    )


def test_whole_class_component_seeds_scaffold_and_flag(tmp_path: Path) -> None:
    good = (
        "class Counter:\n"
        '    """A counter. Starts at zero."""\n'
        "    def incr(self) -> int:\n        return 1\n"
    )
    be = _RecordingBackend(good)
    report = _run_build_with(tmp_path, be)
    assert "pkg.mod" in report.generated
    assert be.seen_ctx is not None
    assert be.seen_ctx.whole_class is True
    assert "class Counter" in be.seen_ctx.seed_target_content
    assert "Counter.incr" in be.seen_ctx.whole_class_contract_block


def test_in_loop_validator_rejects_unfilled_stub(tmp_path: Path) -> None:
    # incr left as a stub: the class-aware retry validator must flag it, so the build fails.
    stub_out = (
        "class Counter:\n"
        '    """A counter. Starts at zero."""\n'
        "    def incr(self) -> int:\n        raise NotImplementedError\n"
    )
    report = _run_build_with(tmp_path, _RecordingBackend(stub_out))
    assert "pkg.mod" in report.failed
    assert any("stub" in e for e in report.failed["pkg.mod"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_builder_whole_class.py -q -k "seeds_scaffold or in_loop_validator"`
Expected: FAIL — `seen_ctx.whole_class` is `False` / `seed_target_content` empty; the stub build currently passes (retry validator is contract-only).

- [ ] **Step 3: Populate the new `_class_validation_inputs` keys**

In `src/jaunt/builder.py`, `_class_validation_inputs`, extend the import and the returned dict. Add to the imports inside the function:

```python
    from jaunt.class_analysis import classify_class_mode
```

Compute class attributes as a `name -> normalized source` dict from the spec class node
(matching Task 1's `class_attributes: dict[str, str]`; add before the `return`):

```python
    class_attributes: dict[str, str] = {}
    for node in cls_node.body:
        if isinstance(node, _ast.Assign):
            rendered = _ast.unparse(node)
            for t in node.targets:
                if isinstance(t, _ast.Name):
                    class_attributes[t.id] = rendered
        elif isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
            class_attributes[node.target.id] = _ast.unparse(node)
```

And add two keys to the returned dict:

```python
        "class_attributes": class_attributes,
        "require_public_method": classify_class_mode(cls_node) == "docstring_only",
```

- [ ] **Step 4: Build the scaffold + contract in `_component_payload`**

In `_component_payload`, immediately before the `ctx = ModuleSpecContext(...)` construction, insert:

```python
            whole = _whole_class_specs(component_entries)
            seed_target_content = ""
            whole_class_contract_block = ""
            if whole:
                from jaunt.class_analysis import (
                    build_class_scaffold,
                    collect_spec_module_imports,
                    render_whole_class_contract,
                    resolve_base_contract,
                )

                spec_src = Path(component_entries[0].source_file).read_text(encoding="utf-8")
                imports = collect_spec_module_imports(spec_src)
                scaffolds = [
                    build_class_scaffold(extract_source_segment(e)) for e in whole.values()
                ]
                seed_parts: list[str] = []
                if imports:
                    seed_parts.append("\n".join(imports))
                seed_parts.extend(scaffolds)
                seed_target_content = "\n\n\n".join(seed_parts).rstrip() + "\n"
                whole_class_contract_block = "\n\n".join(
                    render_whole_class_contract(
                        class_segment=extract_source_segment(e),
                        base_contract_block=resolve_base_contract(e.obj).block,
                    )
                    for e in whole.values()
                )
```

Then add the three fields to the `ModuleSpecContext(...)` call (after `async_runner=async_runner,`):

```python
                seed_target_content=seed_target_content,
                whole_class_contract_block=whole_class_contract_block,
                whole_class=bool(whole),
```

- [ ] **Step 5: Make the retry validator class-aware**

In `_make_validators`, replace the body of `_retry_validator` so it runs the class structural checks (mirroring `_validate_candidate`, but it intentionally keeps the existing ty step last):

```python
            def _retry_validator(source: str) -> list[str]:
                errs = validate_build_contract_only(
                    source,
                    expected_names=component_expected,
                    spec_module=module_name,
                    handwritten_names=handwritten_names,
                )
                if errs:
                    return errs
                whole = _whole_class_specs(component_entries)
                for entry in whole.values():
                    kw = _class_validation_inputs(entry)
                    class_errs = validate_build_class_source(source, **kw)  # type: ignore[arg-type]
                    if class_errs:
                        return class_errs
                if ty_validator is None:
                    return []
                return ty_validator(source)
```

> This is the Codex #2 fix: `generate_with_retry` is given `retry_validator` as its
> `extra_validator`, so the class shape is now checked **inside** the retry loop. A
> missing method / dropped base / unfilled stub becomes a `"contract"` failure that
> `_plan_attempt` (Task 4) escalates to whole-file, instead of only being caught
> post-merge after the loop has already returned.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_builder_whole_class.py -q`
Expected: PASS (new + existing).

- [ ] **Step 7: Lint, typecheck, full suite**

Run: `uv run ruff check src/jaunt/builder.py tests/test_builder_whole_class.py && uv run ty check && uv run pytest -q`
Expected: clean; all pass.

- [ ] **Step 8: Commit**

```bash
git add src/jaunt/builder.py tests/test_builder_whole_class.py
git commit -m "feat(builder): seed per-component scaffold/contract and run class validator in the retry loop"
```

---

## Task 6: Legacy fallback — `run_build(..., fallback_backend=...)` + cli/tester injection

**Files:**
- Modify: `src/jaunt/builder.py` (`run_build` signature ~996-1019; `build_one` final failure path ~1411-1428; add module logger near top)
- Modify: `src/jaunt/cli.py` (`_build_backend` ~528-549; build `run_build` call ~1369-1390; `RepairBuildContext` construction ~1676-1689)
- Modify: `src/jaunt/tester.py` (`RepairBuildContext` ~577-591; its `run_build` call ~1130-1176)
- Test: `tests/test_builder_whole_class.py`

**Interfaces:**
- Consumes: a second `GeneratorBackend` (the direct backend for `cfg.llm.provider`).
- Produces: `run_build(..., fallback_backend: GeneratorBackend | None = None)`. When the aider path fails for a module that contains a whole-class spec, the builder retries that module once with `fallback_backend`, **bypassing the response cache** (it calls `generate_with_retry` directly, never `_generate_ctx`). The fallback source is written through the normal path. `cli._build_fallback_backend(cfg)` returns the direct backend when the engine is aider, else `None`.

> **Decision (refines spec §4 step 3 stamping):** the fallback-written module is stamped
> with the **same build `generation_fingerprint` run_build already holds** (the aider one),
> not a separately-computed legacy fingerprint. Rationale: the module belongs to an
> aider-engine project; stamping the aider fingerprint keeps `jaunt status` stable so the
> module is not perpetually re-flagged stale and re-fallback'd on every build. The real
> corruption vector Codex #5 flagged — poisoning the aider response cache with
> direct-backend output — is closed by the **cache bypass**. Provenance is surfaced via a
> `logger.warning`. (Flag this choice at plan review; reverting to a legacy-fingerprint
> stamp is a localized change if preferred, at the cost of perpetual re-fallback.)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_builder_whole_class.py`:

```python
class _FailingBackend(GeneratorBackend):
    """Aider stand-in that always returns an invalid (empty) whole-class build."""

    @property
    def model_name(self) -> str:
        return "aider"

    @property
    def provider_name(self) -> str:
        return "aider"

    async def generate_module(self, ctx, *, extra_error_context=None):
        return "", None


def test_fallback_recovers_bypasses_cache_and_stamps_aider_fingerprint(tmp_path: Path) -> None:
    from jaunt.cache import ResponseCache
    from jaunt.header import extract_generation_fingerprint

    good = (
        "class Counter:\n"
        '    """A counter. Starts at zero."""\n'
        "    def incr(self) -> int:\n        return 1\n"
    )
    fallback = _StubBackend(good)
    spec_path = _write_spec(tmp_path)
    entry = _entry(spec_path)
    specs = {entry.spec_ref: entry}
    cache = ResponseCache(tmp_path / ".jaunt" / "cache", enabled=True)
    report = asyncio.run(
        run_build(
            package_dir=tmp_path / "src",
            generated_dir="__generated__",
            module_specs={"pkg.mod": [entry]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"pkg.mod": set()},
            stale_modules={"pkg.mod"},
            backend=_FailingBackend(),
            fallback_backend=fallback,
            generation_fingerprint="AIDERFP",
            response_cache=cache,
        )
    )
    assert "pkg.mod" in report.generated
    out = (tmp_path / "src" / "pkg" / "__generated__" / "mod.py").read_text()
    assert "def incr" in out and "return 1" in out
    # Cache-bypass: the failing aider attempt cached nothing and the fallback writes
    # directly, so no entry was persisted.
    assert cache.info()["entries"] == 0
    # Stamp: header carries the (aider) build fingerprint passed to run_build, not the
    # fallback backend's — keeps `status` stable (see the Task 6 decision note).
    assert extract_generation_fingerprint(out) == "sha256:AIDERFP"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_builder_whole_class.py -q -k fallback`
Expected: FAIL — `run_build() got an unexpected keyword argument 'fallback_backend'`.

- [ ] **Step 3: Add a module logger and the `run_build` parameter**

In `src/jaunt/builder.py`, near the top-level imports add (if not already present):

```python
import logging

logger = logging.getLogger("jaunt.builder")
```

In the `run_build` signature, add the parameter (after `backend: GeneratorBackend,`):

```python
    fallback_backend: GeneratorBackend | None = None,
```

- [ ] **Step 4: Add the fallback rung in `build_one`**

In `build_one`, define a fallback helper just before the final single-component attempt block (`if result_source is None:` ~line 1411):

```python
        async def _try_fallback() -> str | None:
            if fallback_backend is None or not _whole_class_specs(entries):
                return None
            fb_ctx, _exp, fb_hw = _component_payload(entries)
            fb_validate_candidate, fb_retry_validator = _make_validators(
                component_entries=entries,
                component_expected=expected,
                handwritten_names=fb_hw,
            )
            _phase(module_name, "fallback", fallback_backend.provider_name)
            logger.warning(
                "whole-class aider build failed for %s; retrying with fallback backend %s",
                module_name,
                fallback_backend.provider_name,
            )
            # NB: call generate_with_retry directly — never _generate_ctx — so the aider
            # response cache is neither read nor written with fallback output.
            result = await fallback_backend.generate_with_retry(
                fb_ctx,
                max_attempts=2,
                extra_validator=fb_retry_validator,
                initial_error_context=(initial_error_context_by_module or {}).get(module_name),
            )
            if result.source is None or result.errors:
                return None
            candidate_errors = fb_validate_candidate(result.source)
            if candidate_errors:
                return None
            if cost_tracker is not None and result.usage is not None:
                cost_tracker.record(module_name, result.usage)
            return result.source
```

Then change the final single-component failure return so it tries the fallback first. Replace:

```python
            if not ok or source is None:
                if split_errors:
                    return False, [*split_errors, *errs]
                return False, errs
            result_source = source
```

with:

```python
            if not ok or source is None:
                fb_source = await _try_fallback()
                if fb_source is None:
                    if split_errors:
                        return False, [*split_errors, *errs]
                    return False, errs
                result_source = fb_source
            else:
                result_source = source
```

> The header write below this block already uses the passed-in `generation_fingerprint`
> (the aider one) — that satisfies the stamping decision noted above. No header change.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_builder_whole_class.py -q -k fallback`
Expected: PASS.

- [ ] **Step 6: Inject the fallback from the CLI**

In `src/jaunt/cli.py`, refactor `_build_backend` into a direct-backend helper + a fallback helper:

```python
def _build_direct_backend(cfg: JauntConfig):
    provider = cfg.llm.provider
    if provider == "openai":
        from jaunt.generate.openai_backend import OpenAIBackend

        return OpenAIBackend(cfg.llm, cfg.prompts)
    if provider == "anthropic":
        from jaunt.generate.anthropic_backend import AnthropicBackend

        return AnthropicBackend(cfg.llm, cfg.prompts)
    if provider == "cerebras":
        from jaunt.generate.cerebras_backend import CerebrasBackend

        return CerebrasBackend(cfg.llm, cfg.prompts)
    raise JauntConfigError(
        f"Unsupported llm.provider: {provider!r}. Supported: 'openai', 'anthropic', 'cerebras'."
    )


def _build_backend(cfg: JauntConfig):
    if cfg.agent.engine == "aider":
        from jaunt.generate.aider_backend import AiderGeneratorBackend

        return AiderGeneratorBackend(cfg.llm, cfg.aider, cfg.prompts)
    return _build_direct_backend(cfg)


def _build_fallback_backend(cfg: JauntConfig):
    """Direct backend used to recover failed whole-class aider builds (None unless aider)."""
    if cfg.agent.engine != "aider":
        return None
    return _build_direct_backend(cfg)
```

In the build command's `builder.run_build(...)` call (~1369), add:

```python
            fallback_backend=_build_fallback_backend(cfg),
```

- [ ] **Step 7: Forward the fallback through the test-repair path**

In `src/jaunt/tester.py`, add a field to `RepairBuildContext`:

```python
    fallback_backend: GeneratorBackend | None = None
```

In tester's `builder.run_build(...)` repair call (~1130), add:

```python
                fallback_backend=repair_build_context.fallback_backend,
```

In `src/jaunt/cli.py`, where `RepairBuildContext(...)` is constructed (~1676), add:

```python
            fallback_backend=_build_fallback_backend(cfg),
```

- [ ] **Step 8: Lint, typecheck, full suite**

Run: `uv run ruff check src/jaunt/builder.py src/jaunt/cli.py src/jaunt/tester.py tests/test_builder_whole_class.py && uv run ty check && uv run pytest -q`
Expected: clean; all pass.

- [ ] **Step 9: Commit**

```bash
git add src/jaunt/builder.py src/jaunt/cli.py src/jaunt/tester.py tests/test_builder_whole_class.py
git commit -m "feat(builder,cli): inject direct-backend fallback for failed whole-class aider builds (cache-bypassing)"
```

---

## Task 7: Flip the example to aider, update docs/memory, verify end-to-end

**Files:**
- Modify: `examples/06_whole_class/jaunt.toml`
- Modify: `examples/06_whole_class/README.md`
- Modify: `/home/diwank/.claude/projects/-home-diwank-github-com-creatorrr-jaunt/memory/aider-whole-class-gap.md` and `MEMORY.md`

**Interfaces:** none (configuration, docs, and a real-LLM smoke test).

- [ ] **Step 1: Remove the legacy pin from the example**

In `examples/06_whole_class/jaunt.toml`, delete the comment + `[agent]` block:

```toml
# Whole-class @magic generation requires the direct ("legacy") backend; the
# default aider engine emits SEARCH/REPLACE edits that don't produce a full
# class body for whole-class specs.
[agent]
engine = "legacy"
```

Leave the rest of the file (the `[llm]` provider stays `openai`/`gpt-5.2` as the documented default; the end-to-end run in Step 4 overrides it via env/flags).

- [ ] **Step 2: Update the example README**

In `examples/06_whole_class/README.md`, delete the `> **Note:** jaunt.toml sets [agent] engine = "legacy" ...` block (lines ~12-14). Whole-class now works under the default engine.

- [ ] **Step 3: Run the existing suite (no regressions)**

Run: `uv run pytest -q && uv run ruff check . && uv run ty check`
Expected: all green.

- [ ] **Step 4: End-to-end with a real LLM (default aider engine)**

The env's `OPENAI_API_KEY` is invalid; use Anthropic. From the repo root:

```bash
cd examples/06_whole_class
JAUNT_GENERATED_DIR=__generated__ \
  uv run --project ../.. jaunt build \
  --config /dev/stdin <<'TOML'
version = 1
[paths]
source_roots = ["src"]
test_roots = ["tests"]
generated_dir = "__generated__"
[llm]
provider = "anthropic"
model = "claude-sonnet-4-6"
api_key_env = "ANTHROPIC_API_KEY"
[test]
pytest_args = ["-q"]
auto_class_tests = true
TOML
```

If `--config /dev/stdin` is not supported by the CLI, instead temporarily set the example `jaunt.toml`'s `[llm]` to the Anthropic block above, run `uv run --project ../.. jaunt build`, then revert the `[llm]` change before committing (Step 1's file must ship with the documented `openai` default and no `[agent]` block).

Expected: `jaunt build` exits 0; `__generated__/specs.py` contains full bodies for `Stack` (push/pop/peek implemented, `is_empty` **verbatim**: `return self._n == 0` or equivalent preserved text), a designed public API for `Inventory`, and `TempStats`.

- [ ] **Step 5: Verify preservation and tests**

```bash
cd examples/06_whole_class
grep -n "is_empty" src/whole_class_demo/__generated__/specs.py   # adjust path to actual generated file
uv run --project ../.. jaunt test --no-build   # uses the build artifact from Step 4
```

Expected: `is_empty` body is byte-for-byte the hand-written version; `jaunt test` generates `TempStats` class tests and the baseline pytest suite passes (the run reported 3 passing tests for `TempStats` under the legacy engine — parity expected under aider).

> If Step 4/5 reveal a prompt/scaffold gap (e.g. docstring-only `Inventory` still thin),
> that is real signal — capture the failing generated output, and fix it in `class_analysis`
> (scaffold/contract) or the aider escalation, not by re-pinning legacy. Re-run Steps 4-5.

- [ ] **Step 6: Update memory (gap closed)**

Edit `/home/diwank/.claude/projects/-home-diwank-github-com-creatorrr-jaunt/memory/aider-whole-class-gap.md`: change the body to record that whole-class `@magic` now works under the default aider engine via scaffold-seeding + in-loop class validation + whole-file escalation + direct-backend fallback (this plan), and that `examples/06_whole_class` no longer pins legacy. Update the matching one-liner in `MEMORY.md` (drop or rephrase the "Gotchas" entry).

- [ ] **Step 7: Commit**

```bash
git add examples/06_whole_class/jaunt.toml examples/06_whole_class/README.md
git commit -m "feat(examples): whole-class example runs under default aider engine; drop legacy pin"
```

(Memory files are outside the repo; they are saved via the memory tool, not committed.)

---

## Self-Review

**Spec coverage:**
- §1 scaffold (imports / header / docstring / attrs / preserved / sentinel stubs; docstring-only `pass`; per-component scope; gating) → Task 2 (functions) + Task 5 (per-component assembly + `whole_class` gating). ✓
- §2 rich contract carried on ctx (not the static no-ctx addendum) → Task 2 renderer + Task 5 (builder renders, populates `whole_class_contract_block`) + Task 4 (written as a `context/` file). ✓
- §3 backend wiring (`seed_target_content`, `whole_class_contract_block`, `whole_class`) + cache key (Codex #6) → Task 3 + Task 4. ✓
- §4 reliability ladder: in-loop class validator (Codex #2) → Task 5; whole-file escalation → Task 4; injected fallback with cache-bypass + stamping (Codex #4/#5) → Task 6. ✓
- §5 guards: AST unfilled-stub (Codex #3), docstring-only completeness (Codex #1), attribute preservation (Codex #9) → Task 1. ✓
- §6 deliverables file-by-file → covered across Tasks 1-7. (The backend `generation_fingerprint(ctx)` method is intentionally **not** modified: it is never called in the build flow — headers/cache use `generation_fingerprint_from_config` — so Codex #6 is satisfied by the `cache_key_from_context` change alone. Noted here to avoid a confused "missing" reviewer flag.)
- §7 testing (unit per layer + e2e via Anthropic) → Tasks 1-6 unit, Task 7 e2e. ✓

**Placeholder scan:** no TBD/TODO; every code step shows full code; every command has expected output. ✓

**Type consistency:** `build_class_scaffold(class_segment: str) -> str`, `collect_spec_module_imports(spec_source: str) -> list[str]`, `render_whole_class_contract(*, class_segment, base_contract_block) -> str`, `validate_build_class_source(..., class_attributes=None, require_public_method=False)`, ctx fields `seed_target_content`/`whole_class_contract_block`/`whole_class`, and `run_build(..., fallback_backend=None)` are used identically wherever they appear. ✓

**Known trade-off (flagged for plan review):** Task 6 stamps fallback output with the aider fingerprint (not a legacy one) + cache-bypass, refining spec §4 step 3 to avoid perpetual re-fallback. See the Task 6 decision note. (User-approved 2026-06-24; spec §4 updated to match.)

## Codex review incorporated (2026-06-24)

A read-only Codex pass (high effort) reviewed this plan against the codebase. The
escalation wiring (Task 5 in-loop validator → Task 4 whole-file escalation) was traced
and confirmed sound. All nine findings were verified against the code and folded in:

- **#1 (BLOCKER)** docstring-only scaffold never emitted `pass` (the `if not new_body:`
  guard was always false once a docstring was added) → Task 2 now emits `pass` when the
  class declares no methods.
- **#2/#3 (MAJOR)** the `# preserved` string-expr marker + global `.replace()` was fragile
  (became the class docstring; corrupted matching string literals) → marker removed; the
  contract block already names preserved methods (Task 2).
- **#4 (MAJOR)** preserved-method normalization in the seed → documented as intentional
  and consistent with the AST-equivalence preserved-method validator (Task 2 note).
- **#5 (MAJOR)** attribute guard checked names only → now compares annotation/value via
  normalized source (Task 1: `class_attributes: dict[str, str]`, `_class_attribute_nodes`).
- **#6 (MAJOR)** plan/spec contradiction on fallback stamping → resolved per the user's
  decision (aider stamp); spec §4 updated to match.
- **#7 (MINOR)** `PromptsConfig()` needs four fields → Task 4 test fixed.
- **#8 (MINOR)** `build_spec_graph` needs `infer_default=` → Tasks 5/6 tests fixed.
- **#9 (MINOR)** fallback test was false-green → Task 6 test now asserts cache stays empty
  and the header carries the aider fingerprint.
