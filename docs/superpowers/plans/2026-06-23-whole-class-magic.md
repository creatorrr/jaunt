# Whole-class `@jaunt.magic` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote whole-class `@jaunt.magic` to a first-class, tested authoring mode (three modes: docstring-only / stubs / mix) with a `@jaunt.preserve` override, inheritance-aware single-shot generation, structural validation, and class-aware auto-testing (explicit + opt-in implicit).

**Architecture:** A `@magic`-decorated class is one spec generated in a single LLM call. A body-shape heuristic (overridable by `@jaunt.preserve`) splits members into *generate* (stubs) and *preserve* (verbatim). Build context carries the resolved base-class/MRO contract. A class-aware validator enforces structure, abstractmethods, preserved-intact, and docstring retention (signatures warn-only). Test generation sources a class target's public surface from the *generated* implementation (hybrid with spec docstrings) and can be triggered explicitly via `@jaunt.test(targets=Cls)` or implicitly via `@jaunt.magic(test=True)`.

**Tech Stack:** Python 3.12+, `ast`, pytest, ruff (line-length 100, rules E/F/I/UP/B), `ty` type checker, `uv`.

## Global Constraints

- Python 3.12+; ruff line-length 100, rules E/F/I/UP/B; `from __future__ import annotations` at top of every module (match existing files).
- Run `uv run pytest`, `uv run ruff check .`, `uv run ty check` after each task; all must pass.
- Tests use mocking for LLM calls and never require API keys.
- The unit-test suite for jaunt itself lives in `tests/`; name new files `tests/test_<area>.py` matching existing convention (`test_validation.py`, `test_builder_methods.py`, `test_module_api.py`, etc.).
- Validator functions return `list[str]` of **hard errors** (empty = ok); warnings are returned by separate `*_warnings` helpers and never fail the build.
- Preserve the existing per-method `@magic` path and the whole-class-vs-per-method conflict rule (`builder.py:781-787`) unchanged.
- Conventional commits (`feat:`, `test:`, `docs:`, `refactor:`). Commit after every task.

---

# Part A — Whole-class generation (independently shippable)

## File Structure (Part A)

- **Create** `src/jaunt/class_analysis.py` — mode detection, stub heuristic, `@jaunt.preserve` AST detection, stub/preserved split, base-class/MRO contract resolution. One responsibility: *static analysis of a `@magic` class body*.
- **Modify** `src/jaunt/runtime.py` — add `preserve` decorator; add `test` kwarg to `magic()`; register project-spec base classes as `auto_deps` for whole-class specs.
- **Modify** `src/jaunt/__init__.py` — export `preserve`.
- **Modify** `src/jaunt/validation.py` — add `validate_build_class_source` + `class_build_warnings`.
- **Modify** `src/jaunt/builder.py` — whole-class build branch: inject base-class contract block, use the class validator.
- **Modify** `src/jaunt/prompts/build_module.md` — whole-class generation section.
- **Create** `examples/06_whole_class/` — runnable example (stubs / mix / docstring-only).
- **Modify** `CLAUDE.md` and `.claude/skills/` jaunt docs.
- **Tests:** `tests/test_class_analysis.py`, `tests/test_preserve_decorator.py`, `tests/test_validation_class.py`, plus additions to `tests/test_magic_decorator.py` and a builder integration test.

---

### Task A1: Stub heuristic + mode detection + member split

**Files:**
- Create: `src/jaunt/class_analysis.py`
- Test: `tests/test_class_analysis.py`

**Interfaces:**
- Produces:
  - `is_preserve_decorator(dec: ast.expr) -> bool`
  - `is_stub_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool`
  - `@dataclass(frozen=True) MemberSplit` with fields `stubs: tuple[str, ...]`, `preserved: tuple[str, ...]`, `preserve_marked: tuple[str, ...]`
  - `split_class_members(class_node: ast.ClassDef) -> MemberSplit`
  - `classify_class_mode(class_node: ast.ClassDef) -> Literal["docstring_only", "stubs", "mix"]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_class_analysis.py
from __future__ import annotations

import ast

from jaunt.class_analysis import (
    MemberSplit,
    classify_class_mode,
    is_preserve_decorator,
    is_stub_body,
    split_class_members,
)


def _cls(src: str) -> ast.ClassDef:
    node = ast.parse(src).body[0]
    assert isinstance(node, ast.ClassDef)
    return node


def test_is_stub_body_recognizes_emptyish_bodies() -> None:
    for body in ("...", "pass", "raise NotImplementedError", "raise NotImplementedError('x')"):
        cls = _cls(f"class C:\n    def m(self):\n        {body}\n")
        fn = cls.body[0]
        assert isinstance(fn, ast.FunctionDef)
        assert is_stub_body(fn) is True


def test_is_stub_body_recognizes_docstring_plus_ellipsis() -> None:
    cls = _cls('class C:\n    def m(self):\n        "doc"\n        ...\n')
    fn = cls.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert is_stub_body(fn) is True


def test_is_stub_body_rejects_real_body() -> None:
    cls = _cls("class C:\n    def m(self):\n        return 1\n")
    fn = cls.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert is_stub_body(fn) is False


def test_preserve_decorator_detected_both_forms() -> None:
    cls = _cls(
        "class C:\n"
        "    @jaunt.preserve\n"
        "    def a(self): ...\n"
        "    @preserve()\n"
        "    def b(self): ...\n"
        "    @other\n"
        "    def c(self): ...\n"
    )
    decs = {fn.name: fn.decorator_list for fn in cls.body if isinstance(fn, ast.FunctionDef)}
    assert any(is_preserve_decorator(d) for d in decs["a"])
    assert any(is_preserve_decorator(d) for d in decs["b"])
    assert not any(is_preserve_decorator(d) for d in decs["c"])


def test_split_class_members_uses_heuristic_and_preserve() -> None:
    cls = _cls(
        "class C:\n"
        '    """spec"""\n'
        "    X = 1\n"
        "    def stub(self): ...\n"
        "    def real(self):\n        return 2\n"
        "    @jaunt.preserve\n"
        "    def kept_stub(self): ...\n"
    )
    split = split_class_members(cls)
    assert split == MemberSplit(
        stubs=("stub",),
        preserved=("kept_stub", "real"),
        preserve_marked=("kept_stub",),
    )


def test_classify_class_mode() -> None:
    docstring_only = _cls('class C:\n    """just a spec"""\n')
    stubs = _cls("class C:\n    def a(self): ...\n    def b(self): ...\n")
    mix = _cls("class C:\n    def a(self): ...\n    def b(self):\n        return 1\n")
    assert classify_class_mode(docstring_only) == "docstring_only"
    assert classify_class_mode(stubs) == "stubs"
    assert classify_class_mode(mix) == "mix"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_class_analysis.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jaunt.class_analysis'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/jaunt/class_analysis.py
"""Static analysis of a @magic class body: modes, stub heuristic, member split."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Literal


def is_preserve_decorator(dec: ast.expr) -> bool:
    """True for ``@jaunt.preserve``, ``@preserve``, or their called forms."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id == "jaunt"
            and target.attr == "preserve"
        )
    if isinstance(target, ast.Name):
        return target.id == "preserve"
    return False


def is_stub_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the body is only docstring / ``...`` / ``pass`` / ``raise NotImplementedError``."""
    for stmt in node.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            # docstring or a bare ``...`` (Ellipsis is a Constant in 3.8+).
            continue
        if isinstance(stmt, ast.Raise) and _is_not_implemented(stmt):
            continue
        return False
    return True


def _is_not_implemented(node: ast.Raise) -> bool:
    exc = node.exc
    if exc is None:
        return False
    if isinstance(exc, ast.Name):
        return exc.id == "NotImplementedError"
    if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
        return exc.func.id == "NotImplementedError"
    return False


@dataclass(frozen=True, slots=True)
class MemberSplit:
    stubs: tuple[str, ...]
    preserved: tuple[str, ...]
    preserve_marked: tuple[str, ...]


def _iter_methods(
    class_node: ast.ClassDef,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in class_node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def split_class_members(class_node: ast.ClassDef) -> MemberSplit:
    stubs: list[str] = []
    preserved: list[str] = []
    preserve_marked: list[str] = []
    for fn in _iter_methods(class_node):
        marked = any(is_preserve_decorator(d) for d in fn.decorator_list)
        if marked:
            preserve_marked.append(fn.name)
            preserved.append(fn.name)
        elif is_stub_body(fn):
            stubs.append(fn.name)
        else:
            preserved.append(fn.name)
    return MemberSplit(
        stubs=tuple(sorted(stubs)),
        preserved=tuple(sorted(preserved)),
        preserve_marked=tuple(sorted(preserve_marked)),
    )


def classify_class_mode(class_node: ast.ClassDef) -> Literal["docstring_only", "stubs", "mix"]:
    methods = _iter_methods(class_node)
    if not methods:
        return "docstring_only"
    split = split_class_members(class_node)
    if split.stubs and not split.preserved:
        return "stubs"
    if not split.stubs and split.preserved:
        return "mix"  # all-real class under @magic is still "mix" (nothing to generate but bodies)
    return "mix"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_class_analysis.py -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check --fix src/jaunt/class_analysis.py tests/test_class_analysis.py
uv run ruff format src/jaunt/class_analysis.py tests/test_class_analysis.py
uv run ty check
git add src/jaunt/class_analysis.py tests/test_class_analysis.py
git commit -m "feat: class-analysis util (stub heuristic, mode detection, member split)"
```

---

### Task A2: `@jaunt.preserve` decorator + export

**Files:**
- Modify: `src/jaunt/runtime.py` (add `preserve` near the `magic` factory, ~line 153)
- Modify: `src/jaunt/__init__.py:13,26-36`
- Test: `tests/test_preserve_decorator.py`

**Interfaces:**
- Produces: `jaunt.preserve` — identity decorator accepting bare (`@preserve`) and called (`@preserve()`) forms.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_preserve_decorator.py
from __future__ import annotations

import jaunt


def test_preserve_bare_is_identity() -> None:
    def f() -> int:
        return 1

    assert jaunt.preserve(f) is f


def test_preserve_called_is_identity() -> None:
    def f() -> int:
        return 1

    assert jaunt.preserve()(f) is f


def test_preserve_exported() -> None:
    assert "preserve" in jaunt.__all__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_preserve_decorator.py -v`
Expected: FAIL with `AttributeError: module 'jaunt' has no attribute 'preserve'`.

- [ ] **Step 3: Implement**

Add to `src/jaunt/runtime.py` (after the `magic(...)` factory definition):

```python
def preserve(fn: F | None = None) -> F | Callable[[F], F]:
    """Mark a method inside a whole-class ``@magic`` as preserved-verbatim.

    Build-time directive only; at runtime the whole class is substituted, so this
    is an identity decorator. Accepts ``@jaunt.preserve`` and ``@jaunt.preserve()``.
    """
    if fn is None:
        def _decorate(f: F) -> F:
            return f

        return _decorate
    return fn
```

In `src/jaunt/__init__.py`, change the import and `__all__`:

```python
from jaunt.runtime import magic, preserve, test
```

```python
__all__ = [
    "__version__",
    "magic",
    "preserve",
    "test",
    "JauntError",
    "JauntConfigError",
    "JauntDiscoveryError",
    "JauntNotBuiltError",
    "JauntGenerationError",
    "JauntDependencyCycleError",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_preserve_decorator.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check --fix src/jaunt/runtime.py src/jaunt/__init__.py tests/test_preserve_decorator.py
uv run ty check
git add src/jaunt/runtime.py src/jaunt/__init__.py tests/test_preserve_decorator.py
git commit -m "feat: add and export @jaunt.preserve identity decorator"
```

---

### Task A3: `magic(test=...)` kwarg pass-through

**Files:**
- Modify: `src/jaunt/runtime.py` (`magic` factory signature ~line 153 and the `decorator_kwargs` block ~line 179-185)
- Test: add to `tests/test_magic_decorator.py`

**Interfaces:**
- Produces: `@jaunt.magic(test=True)` stores `decorator_kwargs["test"] = True` on the class's `SpecEntry`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_magic_decorator.py
def test_magic_test_kwarg_recorded() -> None:
    from jaunt.registry import clear_registries, get_magic_registry
    from jaunt.runtime import magic
    from jaunt.spec_ref import normalize_spec_ref

    clear_registries()

    @magic(test=True)
    class WithAutoTest:
        """spec"""

    ref = normalize_spec_ref(f"{WithAutoTest.__module__}:WithAutoTest")
    entry = get_magic_registry()[ref]
    assert entry.decorator_kwargs.get("test") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_magic_decorator.py::test_magic_test_kwarg_recorded -v`
Expected: FAIL with `TypeError: magic() got an unexpected keyword argument 'test'`.

- [ ] **Step 3: Implement**

In `src/jaunt/runtime.py`, change the `magic` signature:

```python
def magic(
    *,
    deps: object | None = None,
    prompt: object | None = None,
    infer_deps: object | None = None,
    test: object | None = None,
):
```

And in the `decorator_kwargs` assembly block (after the `infer_deps` handling):

```python
        if test is not None:
            if not isinstance(test, bool):
                raise JauntError("@magic(test=...) must be a boolean when provided.")
            decorator_kwargs["test"] = test
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_magic_decorator.py::test_magic_test_kwarg_recorded -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix src/jaunt/runtime.py tests/test_magic_decorator.py
uv run ty check
git add src/jaunt/runtime.py tests/test_magic_decorator.py
git commit -m "feat: accept @magic(test=...) kwarg for opt-in implicit tests"
```

---

### Task A4: Base-class / MRO contract resolution

**Files:**
- Modify: `src/jaunt/class_analysis.py`
- Test: add to `tests/test_class_analysis.py`

**Interfaces:**
- Consumes: `MemberSplit` (Task A1).
- Produces:
  - `@dataclass(frozen=True) BaseContract` with `block: str` (prompt text), `project_base_refs: tuple[str, ...]` (spec-ref strings for project-spec bases), `required_abstractmethods: tuple[str, ...]`.
  - `resolve_base_contract(cls_obj: type) -> BaseContract` — uses the *live* class object (available at registration time in `runtime.magic`) to walk `cls_obj.__mro__`, collecting public method signatures and `__abstractmethods__`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_class_analysis.py
import abc

from jaunt.class_analysis import BaseContract, resolve_base_contract


def test_resolve_base_contract_collects_abstractmethods_and_signatures() -> None:
    class Base(abc.ABC):
        @abc.abstractmethod
        def required(self, x: int) -> int: ...

        def helper(self) -> str:
            return "h"

    class Child(Base):
        """spec"""

    contract = resolve_base_contract(Child)
    assert isinstance(contract, BaseContract)
    assert "required" in contract.required_abstractmethods
    assert "required" in contract.block
    assert "helper" in contract.block  # inherited public method is offered as context


def test_resolve_base_contract_no_bases() -> None:
    class Plain:
        """spec"""

    contract = resolve_base_contract(Plain)
    assert contract.required_abstractmethods == ()
    # object has no public spec-relevant members worth surfacing
    assert contract.project_base_refs == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_class_analysis.py -k base_contract -v`
Expected: FAIL with `ImportError: cannot import name 'BaseContract'`.

- [ ] **Step 3: Implement** (append to `src/jaunt/class_analysis.py`)

```python
import inspect


@dataclass(frozen=True, slots=True)
class BaseContract:
    block: str
    project_base_refs: tuple[str, ...]
    required_abstractmethods: tuple[str, ...]


def resolve_base_contract(cls_obj: type) -> BaseContract:
    required = tuple(sorted(getattr(cls_obj, "__abstractmethods__", frozenset())))

    project_refs: list[str] = []
    for base in cls_obj.__bases__:
        if base is object:
            continue
        mod = getattr(base, "__module__", "")
        qual = getattr(base, "__qualname__", base.__name__)
        # A project base is any non-stdlib base; record a spec-ref-shaped string.
        if mod and not mod.startswith(("builtins", "abc", "typing", "collections")):
            project_refs.append(f"{mod}:{qual}")

    lines: list[str] = []
    seen: set[str] = set()
    for base in cls_obj.__mro__[1:]:
        if base is object:
            continue
        for name, member in sorted(vars(base).items()):
            if name.startswith("_") and not name.startswith("__"):
                continue
            if name in seen or not callable(member):
                continue
            seen.add(name)
            try:
                sig = str(inspect.signature(member))
            except (TypeError, ValueError):
                sig = "(...)"
            abstract = " [abstractmethod]" if name in required else ""
            lines.append(f"{base.__name__}.{name}{sig}{abstract}")

    block = "\n".join(lines) if lines else "(no base classes)"
    return BaseContract(
        block=block,
        project_base_refs=tuple(project_refs),
        required_abstractmethods=required,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_class_analysis.py -k base_contract -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix src/jaunt/class_analysis.py tests/test_class_analysis.py
uv run ty check
git add src/jaunt/class_analysis.py tests/test_class_analysis.py
git commit -m "feat: resolve base-class/MRO contract for whole-class specs"
```

---

### Task A5: Class-aware build validator

**Files:**
- Modify: `src/jaunt/validation.py`
- Test: `tests/test_validation_class.py`

**Interfaces:**
- Consumes: `MemberSplit`, `BaseContract` (callers pass plain data — see signature below).
- Produces (in `validation.py`):
  - `validate_build_class_source(source, *, class_name, stub_methods, preserved_segments, declared_bases, class_decorators, required_abstractmethods, spec_docstring) -> list[str]`
    - `preserved_segments: dict[str, str]` maps preserved member name → its spec source segment (with any `@jaunt.preserve` stripped) for AST-equivalence comparison.
    - `declared_bases: list[str]`, `class_decorators: list[str]` (unparsed expressions), `required_abstractmethods: list[str]`, `spec_docstring: str`.
  - `class_build_warnings(source, *, class_name, stub_signatures) -> list[str]` where `stub_signatures: dict[str, list[str]]` maps stub name → declared param names; warns on dropped params.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_validation_class.py
from __future__ import annotations

from jaunt.validation import class_build_warnings, validate_build_class_source


BASE_KW = dict(
    class_name="C",
    stub_methods=["do"],
    preserved_segments={},
    declared_bases=[],
    class_decorators=[],
    required_abstractmethods=[],
    spec_docstring="A class.",
)


def test_passes_when_structure_matches() -> None:
    src = 'class C:\n    "A class. Extra notes."\n    def do(self):\n        return 1\n'
    assert validate_build_class_source(src, **BASE_KW) == []


def test_fails_when_stub_method_missing() -> None:
    src = 'class C:\n    "A class."\n    def other(self):\n        return 1\n'
    errs = validate_build_class_source(src, **BASE_KW)
    assert any("do" in e for e in errs)


def test_fails_when_base_dropped() -> None:
    src = 'class C:\n    "A class."\n    def do(self): return 1\n'
    kw = {**BASE_KW, "declared_bases": ["Base"]}
    errs = validate_build_class_source(src, **kw)
    assert any("Base" in e for e in errs)


def test_fails_when_abstractmethod_unimplemented() -> None:
    src = 'class C(Base):\n    "A class."\n    def do(self): return 1\n'
    kw = {**BASE_KW, "declared_bases": ["Base"], "required_abstractmethods": ["needed"]}
    errs = validate_build_class_source(src, **kw)
    assert any("needed" in e for e in errs)


def test_fails_when_preserved_method_drifts() -> None:
    spec_seg = "def kept(self):\n    return 42"
    src = 'class C:\n    "A class."\n    def do(self): return 1\n    def kept(self):\n        return 99\n'
    kw = {**BASE_KW, "preserved_segments": {"kept": spec_seg}}
    errs = validate_build_class_source(src, **kw)
    assert any("kept" in e for e in errs)


def test_passes_when_preserved_method_intact_modulo_formatting() -> None:
    spec_seg = "def kept(self):\n    return 42"
    src = 'class C:\n    "A class."\n    def do(self): return 1\n    def kept(self):\n        return 42\n'
    kw = {**BASE_KW, "preserved_segments": {"kept": spec_seg}}
    assert validate_build_class_source(src, **kw) == []


def test_fails_when_docstring_dropped() -> None:
    src = 'class C:\n    "Totally different."\n    def do(self): return 1\n'
    errs = validate_build_class_source(src, **BASE_KW)
    assert any("docstring" in e.lower() for e in errs)


def test_extra_private_methods_allowed() -> None:
    src = (
        'class C:\n    "A class."\n'
        "    def do(self): return self._helper()\n"
        "    def _helper(self): return 1\n"
    )
    assert validate_build_class_source(src, **BASE_KW) == []


def test_dropped_param_warns_not_fails() -> None:
    src = 'class C:\n    "A class."\n    def do(self): return 1\n'
    # validation passes (warn-only)
    assert validate_build_class_source(src, **BASE_KW) == []
    warns = class_build_warnings(src, class_name="C", stub_signatures={"do": ["self", "x"]})
    assert any("x" in w for w in warns)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validation_class.py -v`
Expected: FAIL with `ImportError: cannot import name 'validate_build_class_source'`.

- [ ] **Step 3: Implement** (append to `src/jaunt/validation.py`)

```python
def _find_class(mod: ast.Module, class_name: str) -> ast.ClassDef | None:
    for node in mod.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _method_nodes(cls: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        n.name: n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _normalized_ast_dump(src_or_node: str | ast.AST) -> str:
    node = ast.parse(src_or_node).body[0] if isinstance(src_or_node, str) else src_or_node
    # Strip decorators so @jaunt.preserve and formatting don't affect equivalence.
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        node.decorator_list = []
    return ast.dump(node, include_attributes=False)


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
) -> list[str]:
    try:
        mod = ast.parse(source or "")
    except SyntaxError as e:
        return [_syntax_error_to_str(e)]

    cls = _find_class(mod, class_name)
    if cls is None:
        return [f"Missing top-level class definition: {class_name}"]

    errors: list[str] = []
    methods = _method_nodes(cls)

    # Structure: stub methods must exist.
    for name in stub_methods:
        if name not in methods:
            errors.append(f"{class_name}: missing required method {name!r} from spec.")

    # Structure: declared bases preserved by name.
    actual_bases = {ast.unparse(b) for b in cls.bases}
    for base in declared_bases:
        if base not in actual_bases:
            errors.append(f"{class_name}: declared base class {base!r} was not preserved.")

    # Structure: class decorators preserved by source text.
    actual_decos = {ast.unparse(d) for d in cls.decorator_list}
    for deco in class_decorators:
        if deco not in actual_decos:
            errors.append(f"{class_name}: class decorator {deco!r} was not preserved.")

    # Abstractmethods: each required name must be defined on the generated class.
    for name in required_abstractmethods:
        if name not in methods:
            errors.append(
                f"{class_name}: inherited abstractmethod {name!r} is not implemented."
            )

    # Preserved-intact: AST-equivalence (decorators stripped).
    for name, spec_seg in preserved_segments.items():
        node = methods.get(name)
        if node is None:
            errors.append(f"{class_name}: preserved method {name!r} is missing from output.")
            continue
        if _normalized_ast_dump(node) != _normalized_ast_dump(spec_seg):
            errors.append(
                f"{class_name}: preserved method {name!r} was modified; it must be kept verbatim."
            )

    # Docstring retained (additions allowed).
    if spec_docstring:
        actual_doc = ast.get_docstring(cls, clean=True) or ""
        if spec_docstring.strip() not in actual_doc:
            errors.append(
                f"{class_name}: the spec docstring must be retained (additions are allowed)."
            )

    return errors


def class_build_warnings(
    source: str,
    *,
    class_name: str,
    stub_signatures: dict[str, list[str]],
) -> list[str]:
    try:
        mod = ast.parse(source or "")
    except SyntaxError:
        return []
    cls = _find_class(mod, class_name)
    if cls is None:
        return []
    methods = _method_nodes(cls)
    warnings: list[str] = []
    for name, declared_params in stub_signatures.items():
        node = methods.get(name)
        if node is None:
            continue
        actual = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
        for param in declared_params:
            if param not in actual:
                warnings.append(
                    f"{class_name}.{name}: generated signature dropped declared parameter {param!r}."
                )
    return warnings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_validation_class.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix src/jaunt/validation.py tests/test_validation_class.py
uv run ty check
git add src/jaunt/validation.py tests/test_validation_class.py
git commit -m "feat: class-aware build validator (structure, abstractmethods, preserved-intact, docstring)"
```

---

### Task A6: Register project-spec base classes as deps for whole-class specs

**Files:**
- Modify: `src/jaunt/runtime.py` (the `isinstance(obj, type)` branch, ~line 215, before the import-substitution attempt)
- Test: add to `tests/test_magic_decorator.py`

**Interfaces:**
- Consumes: `resolve_base_contract` (Task A4).
- Produces: a whole-class `SpecEntry` whose `auto_deps` include any project-spec base-class refs, so `digest.graph_digest` invalidates the subclass when a base spec changes.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_magic_decorator.py
def test_whole_class_records_project_base_dep(monkeypatch) -> None:
    from jaunt.registry import clear_registries, get_magic_registry
    from jaunt.runtime import magic
    from jaunt.spec_ref import normalize_spec_ref

    clear_registries()

    @magic()
    class Base:
        """base spec"""

    @magic()
    class Child(Base):
        """child spec"""

    child_ref = normalize_spec_ref(f"{Child.__module__}:Child")
    entry = get_magic_registry()[child_ref]
    dep_strs = {str(d) for d in entry.auto_deps}
    assert any(d.endswith(":Base") for d in dep_strs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_magic_decorator.py::test_whole_class_records_project_base_dep -v`
Expected: FAIL (no `:Base` dep in `auto_deps`).

- [ ] **Step 3: Implement**

In `src/jaunt/runtime.py`, the `magic` factory currently builds `entry` once for all kinds. For the whole-class case (`isinstance(obj, type)` and `class_name is None`), merge base-class refs into `auto_deps` **before** constructing the `SpecEntry`. Add, right after `analysis = analyze_magic_decorators(...)`:

```python
        merged_auto_deps = analysis.auto_deps
        if isinstance(obj, type) and class_name is None:
            from jaunt.class_analysis import resolve_base_contract
            from jaunt.spec_ref import normalize_spec_ref

            base_refs = []
            for ref_str in resolve_base_contract(obj).project_base_refs:
                try:
                    base_refs.append(normalize_spec_ref(ref_str))
                except Exception:
                    continue
            if base_refs:
                merged = set(analysis.auto_deps) | set(base_refs)
                merged_auto_deps = tuple(sorted(merged, key=str))
```

Then change the `SpecEntry(... auto_deps=analysis.auto_deps ...)` to `auto_deps=merged_auto_deps`.

> Note: only the *registered* base refs are added; non-project (stdlib/abc) bases are excluded by `resolve_base_contract`. The dep is harmless if the base isn't actually a registered spec — `graph_digest` only follows edges present in `spec_graph`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_magic_decorator.py::test_whole_class_records_project_base_dep -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix src/jaunt/runtime.py tests/test_magic_decorator.py
uv run ty check
git add src/jaunt/runtime.py tests/test_magic_decorator.py
git commit -m "feat: whole-class specs depend on project-spec base classes"
```

---

### Task A7: Build branch — inject base contract + use class validator + prompt

**Files:**
- Modify: `src/jaunt/builder.py` (`_validate_module_candidate` ~line 1227 and `_make_validators`/`_validate_candidate` ~line 1180-1211; context assembly `_component_payload` ~line 1114 and `build_module_context_artifacts` ~line 276)
- Modify: `src/jaunt/prompts/build_module.md:14`
- Test: `tests/test_builder_whole_class.py`

**Interfaces:**
- Consumes: `validate_build_class_source`, `class_build_warnings` (A5); `split_class_members`, `resolve_base_contract` (A1/A4); `SpecEntry.obj` (the live class) for whole-class specs.
- Produces: when a module's expected name is a whole-class spec, the build validator is class-aware and the prompt receives the base-class contract.

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_builder_whole_class.py
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jaunt import builder, registry
from jaunt.deps import build_spec_graph, collapse_to_module_dag


class _StubBackend:
    model_name = "stub"
    provider_name = "stub"

    def __init__(self, source: str) -> None:
        self._source = source

    async def generate_with_retry(self, ctx, *, extra_validator=None, initial_error_context=None, progress=None):
        from jaunt.generate.base import GenerationResult

        return GenerationResult(source=self._source, errors=[], usage=None)


def _write_spec(tmp_path: Path) -> Path:
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    spec = pkg / "mod.py"
    spec.write_text(
        "import jaunt\n\n"
        "@jaunt.magic()\n"
        "class Counter:\n"
        '    """A counter. Starts at zero."""\n'
        "    def incr(self) -> int: ...\n"
    )
    return spec


def test_whole_class_build_uses_class_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A generated class that DROPS the stub method must fail class validation.
    registry.clear_registries()
    # (Full wiring: discover, import, build_spec_graph, run_build with _StubBackend.)
    # Assert: building with a backend that omits `incr` reports a failure mentioning 'incr';
    # building with a backend that defines `incr` succeeds.
    ...
```

> The integration test is sketched; flesh it out using the discovery+registry+`run_build` pattern in `tests/test_builder_methods.py` (which already constructs `SpecEntry`s and calls `_build_expected_names`). The key assertions: (a) class validator runs for whole-class specs, (b) a missing stub method fails, (c) a conforming class passes.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_builder_whole_class.py -v`
Expected: FAIL (class validator not yet wired; missing-stub class would pass the old name-only validator).

- [ ] **Step 3: Implement the validator hookup**

In `builder.py`, add a helper that decides whether an expected name is a whole-class spec and, if so, returns the data needed by `validate_build_class_source`. Add near the other module-level helpers:

```python
def _whole_class_specs(entries: list[SpecEntry]) -> dict[str, SpecEntry]:
    """Map class name -> SpecEntry for whole-class @magic specs (obj is a type, no dot)."""
    out: dict[str, SpecEntry] = {}
    for e in entries:
        if e.class_name is None and "." not in e.qualname and isinstance(e.obj, type):
            out[e.qualname] = e
    return out
```

Then build a per-class validation closure. Add a function that, given a whole-class `SpecEntry`, produces the kwargs for `validate_build_class_source` by parsing the spec source segment (reuse `extract_source_segment(entry)`), running `split_class_members`, and `resolve_base_contract(entry.obj)`:

```python
def _class_validation_inputs(entry: SpecEntry) -> dict[str, object]:
    import ast as _ast

    from jaunt.class_analysis import resolve_base_contract, split_class_members
    from jaunt.digest import extract_source_segment

    seg = extract_source_segment(entry)
    cls_node = _ast.parse(seg).body[0]
    assert isinstance(cls_node, _ast.ClassDef)
    split = split_class_members(cls_node)
    methods = {
        n.name: n for n in cls_node.body
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
    }
    preserved_segments: dict[str, str] = {}
    for name in split.preserved:
        node = methods[name]
        # strip @jaunt.preserve before storing for comparison
        from jaunt.class_analysis import is_preserve_decorator
        clone = _ast.parse(_ast.unparse(node)).body[0]
        assert isinstance(clone, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        clone.decorator_list = [d for d in clone.decorator_list if not is_preserve_decorator(d)]
        preserved_segments[name] = _ast.unparse(clone)
    contract = resolve_base_contract(entry.obj)  # type: ignore[arg-type]
    return {
        "class_name": entry.qualname,
        "stub_methods": list(split.stubs),
        "preserved_segments": preserved_segments,
        "declared_bases": [_ast.unparse(b) for b in cls_node.bases],
        "class_decorators": [
            _ast.unparse(d) for d in cls_node.decorator_list
            if not _is_magic_decorator(d)
        ],
        "required_abstractmethods": list(contract.required_abstractmethods),
        "spec_docstring": _ast.get_docstring(cls_node, clean=True) or "",
    }
```

> `_is_magic_decorator` already exists in `decorator_analysis.py`; import it: `from jaunt.decorator_analysis import _is_magic_decorator`.

In both `_validate_candidate` (inside `_make_validators`) and `_validate_module_candidate`, after the existing `validate_build_generated_source(...)` call returns no errors, run class validation for any whole-class specs in scope:

```python
            whole = _whole_class_specs(component_entries)  # or `entries` for the module-level one
            for entry in whole.values():
                kw = _class_validation_inputs(entry)
                class_errs = validate_build_class_source(source, **kw)  # type: ignore[arg-type]
                if class_errs:
                    return class_errs
```

Add the import at the top of `builder.py`:

```python
from jaunt.validation import (
    validate_build_class_source,
    class_build_warnings,
    validate_build_contract_only,
    validate_build_generated_source,
)
```

(Warnings: after a successful module generation, call `class_build_warnings(...)` and log via the existing diagnostics/progress channel — non-failing.)

- [ ] **Step 4: Implement the prompt + base-contract context**

Replace `prompts/build_module.md:14` with a whole-class section:

```markdown
- If a spec shows a class decorated with `@magic` (whole-class mode), generate the COMPLETE class:
  - Implement every method whose body is a stub (only a docstring, `...`, `pass`, or `raise NotImplementedError`).
  - Keep every other method, class attribute, base class, and class decorator EXACTLY as shown (verbatim) — including any method marked `@jaunt.preserve`, which you must emit WITHOUT the `@jaunt.preserve` decorator.
  - You may add private helper methods and shared state as needed.
  - Retain the class docstring's content (you may append notes).
  - Honor the inheritance contract in "Base class contract" below: implement all inherited abstractmethods and make overrides consistent with their base signatures.
  - If the class body is only a docstring (docstring-only mode), design the full public API the docstring implies.
- If a spec shows a class with per-method `@magic` stubs, generate the entire class with those methods implemented (legacy per-method mode), preserving non-magic members and decorators.
```

Add a base-contract block to the prompt. In `ModuleSpecContext` (in `generate/base.py`) add an optional field `base_contract_block: str = ""`, render it in `build_module.md` (e.g. after `{{decorator_apis_block}}`):

```markdown
Base class contract (inherited/overridable methods and required abstractmethods):
{{base_contract_block}}
```

Populate it in `_component_payload` / the module-level context (builder.py ~1157): for any whole-class spec in scope, set `base_contract_block` to `resolve_base_contract(entry.obj).block` (join with blank lines if multiple). Fold its text into `module_context_digest` so a base-contract change invalidates the build (append the block to the digest payload in `build_module_context_artifacts`).

- [ ] **Step 5: Run tests, lint, typecheck, commit**

```bash
uv run pytest tests/test_builder_whole_class.py tests/test_builder_methods.py -v
uv run ruff check --fix src/jaunt/builder.py src/jaunt/prompts/build_module.md tests/test_builder_whole_class.py
uv run ty check
git add src/jaunt/builder.py src/jaunt/prompts/build_module.md src/jaunt/generate/base.py tests/test_builder_whole_class.py
git commit -m "feat: whole-class build branch with class validator and base-class contract"
```

---

### Task A8: Example + docs + full suite (Part A done)

**Files:**
- Create: `examples/06_whole_class/jaunt.toml`, `examples/06_whole_class/src/whole_class_demo/specs.py`, `examples/06_whole_class/README.md`
- Modify: `CLAUDE.md` (Key Concepts: whole-class mode + `@jaunt.preserve`), `.claude/skills/aider.md` or the jaunt skill doc if it documents authoring modes.

- [ ] **Step 1: Write the example specs** (covers all three modes)

```python
# examples/06_whole_class/src/whole_class_demo/specs.py
from __future__ import annotations

import jaunt


@jaunt.magic()
class Stack:
    """A LIFO stack of ints. push/pop/peek; pop and peek raise IndexError when empty."""

    def push(self, value: int) -> None: ...
    def pop(self) -> int: ...
    def peek(self) -> int: ...

    @jaunt.preserve
    def is_empty(self) -> bool:
        """Hand-written: kept verbatim even though it looks tiny."""
        return len(self._items) == 0  # noqa: F821 (the generated class defines _items)


@jaunt.magic()
class Inventory:
    """Docstring-only: an item->quantity store. Supports add(item, qty),
    remove(item, qty) (never below zero), and total() across all items."""
```

> `Stack` shows stubs + `@jaunt.preserve` (mix). `Inventory` shows docstring-only.

- [ ] **Step 2: Build the example to confirm end-to-end**

Run (requires an API key):
`cd examples/06_whole_class && uv run --project ../.. jaunt build`
Expected: `__generated__/whole_class_demo/specs.py` contains a complete `Stack` (with `push/pop/peek`, verbatim `is_empty`) and a designed `Inventory`.

- [ ] **Step 3: Update docs**

Add to `CLAUDE.md` under Key Concepts a short "Whole-class `@magic`" paragraph describing the three modes and `@jaunt.preserve`.

- [ ] **Step 4: Run the full suite**

```bash
uv run pytest
uv run ruff check .
uv run ty check
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add examples/06_whole_class CLAUDE.md
git commit -m "docs: whole-class @magic example and guide"
```

---

# Part B — Auto-testing (builds on Part A)

## File Structure (Part B)

- **Modify** `src/jaunt/module_api.py` — hybrid summary from generated source + generated public-API digest.
- **Modify** `src/jaunt/config.py` — `[test] auto_class_tests` (default false).
- **Modify** `src/jaunt/module_contract.py` + `src/jaunt/tester.py` + `src/jaunt/cli.py` — synthesize virtual test specs for `test=True` classes; deterministic output path; feed hybrid summary + generated-API digest into staleness.
- **Modify** `src/jaunt/prompts/test_module.md`, `src/jaunt/prompts/test_system.md` — class-aware test guidance.
- **Tests:** `tests/test_module_api_generated.py`, `tests/test_config.py` (additions), `tests/test_auto_class_tests.py`.

---

### Task B1: Hybrid API summary from generated source + public-API digest

**Files:**
- Modify: `src/jaunt/module_api.py`
- Test: `tests/test_module_api_generated.py`

**Interfaces:**
- Produces:
  - `build_generated_class_api_summary(generated_source: str, class_name: str, *, spec_docstring: str, public_api_only: bool = True) -> SpecApiSummary` — reads members from the GENERATED class; `doc` uses `spec_docstring` (the contract); members filtered to public when `public_api_only`.
  - `generated_public_api_digest(generated_source: str, class_name: str) -> str` — sha256 over public member names + signatures.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_module_api_generated.py
from __future__ import annotations

from jaunt.module_api import build_generated_class_api_summary, generated_public_api_digest

GEN = (
    "class Inv:\n"
    '    """generated docs"""\n'
    "    def add(self, item, qty): return None\n"
    "    def total(self): return 0\n"
    "    def _bump(self): return 1\n"
)


def test_hybrid_summary_reads_generated_members_uses_spec_doc() -> None:
    s = build_generated_class_api_summary(GEN, "Inv", spec_docstring="SPEC DOC")
    names = {m.name for m in s.members}
    assert names == {"add", "total"}  # private _bump excluded
    assert s.doc == "SPEC DOC"


def test_hybrid_summary_white_box_includes_private() -> None:
    s = build_generated_class_api_summary(GEN, "Inv", spec_docstring="x", public_api_only=False)
    assert "_bump" in {m.name for m in s.members}


def test_generated_public_api_digest_ignores_private_changes() -> None:
    other = GEN.replace("_bump", "_bump2").replace("return 1", "return 2")
    assert generated_public_api_digest(GEN, "Inv") == generated_public_api_digest(other, "Inv")


def test_generated_public_api_digest_changes_on_public_change() -> None:
    changed = GEN.replace("def total(self)", "def total(self, scope)")
    assert generated_public_api_digest(GEN, "Inv") != generated_public_api_digest(changed, "Inv")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_module_api_generated.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement** (append to `src/jaunt/module_api.py`)

```python
def build_generated_class_api_summary(
    generated_source: str,
    class_name: str,
    *,
    spec_docstring: str,
    public_api_only: bool = True,
) -> SpecApiSummary:
    tree = ast.parse(generated_source)
    cls = _find_top_level_class(tree, class_name)
    if cls is None:
        raise ValueError(f"Generated class {class_name!r} not found.")
    members = _class_members(generated_source, cls)
    if public_api_only:
        members = tuple(m for m in members if not m.name.startswith("_") or m.name.startswith("__"))
    return SpecApiSummary(
        spec_ref=SpecRef(f"<generated>:{class_name}"),
        kind="class",
        name=class_name,
        signature=_class_signature(cls),
        doc=spec_docstring,
        members=members,
    )


def generated_public_api_digest(generated_source: str, class_name: str) -> str:
    summary = build_generated_class_api_summary(
        generated_source, class_name, spec_docstring="", public_api_only=True
    )
    payload = json.dumps(
        [[m.name, m.signature] for m in summary.members], sort_keys=True, ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_module_api_generated.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix src/jaunt/module_api.py tests/test_module_api_generated.py
uv run ty check
git add src/jaunt/module_api.py tests/test_module_api_generated.py
git commit -m "feat: hybrid generated-class API summary + public-API digest"
```

---

### Task B2: `[test] auto_class_tests` config

**Files:**
- Modify: `src/jaunt/config.py:52-59` (`TestConfig`), `:295-308` (parsing), `:429` (construction)
- Test: add to `tests/test_config.py`

**Interfaces:**
- Produces: `cfg.test.auto_class_tests: bool` (default `False`).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_config.py
def test_auto_class_tests_defaults_false_and_parses(tmp_path) -> None:
    from jaunt.config import load_config

    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text(
        "version = 1\n[test]\nauto_class_tests = true\n"
    )
    cfg = load_config(root=tmp_path)
    assert cfg.test.auto_class_tests is True

    (tmp_path / "jaunt2.toml").write_text("version = 1\n")
    cfg2 = load_config(config_path=tmp_path / "jaunt2.toml", root=tmp_path)
    assert cfg2.test.auto_class_tests is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_auto_class_tests_defaults_false_and_parses -v`
Expected: FAIL with `AttributeError: 'TestConfig' object has no attribute 'auto_class_tests'`.

- [ ] **Step 3: Implement**

`TestConfig` (config.py:52):

```python
@dataclass(frozen=True)
class TestConfig:
    __test__ = False  # prevent pytest collection

    jobs: int
    infer_deps: bool
    pytest_args: list[str]
    auto_class_tests: bool = False
```

Parsing (after `pytest_args` block, ~config.py:308):

```python
    if "auto_class_tests" in test_tbl:
        auto_class_tests = _as_bool(test_tbl["auto_class_tests"], name="test.auto_class_tests")
    else:
        auto_class_tests = False
```

Construction (config.py:429):

```python
        test=TestConfig(
            jobs=test_jobs,
            infer_deps=test_infer_deps,
            pytest_args=pytest_args,
            auto_class_tests=auto_class_tests,
        ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py::test_auto_class_tests_defaults_false_and_parses -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix src/jaunt/config.py tests/test_config.py
uv run ty check
git add src/jaunt/config.py tests/test_config.py
git commit -m "feat: [test] auto_class_tests config flag"
```

---

### Task B3: Synthesize virtual test specs for `test=True` classes + output path

**Files:**
- Modify: `src/jaunt/module_contract.py` (new `synthesize_auto_class_test_entries`)
- Modify: `src/jaunt/tester.py` (`_resolve_test_output_path` handling for auto entries)
- Test: `tests/test_auto_class_tests.py`

**Interfaces:**
- Consumes: the magic registry (`dict[SpecRef, SpecEntry]`), `cfg.test.auto_class_tests`, each magic class's `decorator_kwargs.get("test")`.
- Produces:
  - `synthesize_auto_class_test_entries(magic_specs: dict[SpecRef, SpecEntry], *, default_on: bool, tests_package: str, generated_dir: str) -> dict[str, list[SpecEntry]]` — returns `module_specs`-shaped dict of synthetic test entries (one per opted-in whole-class spec), each with `kind="test"`, `decorator_kwargs={"targets": (class_ref,), "public_api_only": True}`, a synthetic `module` name like `<tests_package>.__auto__.<spec_module>`, `qualname=f"test_{ClassName.lower()}_baseline"`, and `source_file` set to the magic spec's `source_file`.
  - These entries write to `<test_root>/<generated_dir>/auto/<spec/module/path>.py` (handled by the tester via the synthetic module name).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auto_class_tests.py
from __future__ import annotations

from jaunt.module_contract import synthesize_auto_class_test_entries
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _magic_class(module: str, name: str, *, test: object) -> SpecEntry:
    kwargs = {} if test is None else {"test": test}
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{name}"),
        module=module,
        qualname=name,
        source_file=f"/src/{module.replace('.', '/')}.py",
        obj=type(name, (), {}),
        decorator_kwargs=kwargs,
        class_name=None,
    )


def test_opt_in_via_kwarg() -> None:
    specs = {e.spec_ref: e for e in [_magic_class("pkg.mod", "Cart", test=True)]}
    out = synthesize_auto_class_test_entries(
        specs, default_on=False, tests_package="tests", generated_dir="__generated__"
    )
    assert len(out) == 1
    entries = next(iter(out.values()))
    assert entries[0].kind == "test"
    assert entries[0].decorator_kwargs["public_api_only"] is True
    targets = {str(t) for t in entries[0].decorator_kwargs["targets"]}
    assert "pkg.mod:Cart" in targets


def test_default_on_applies_when_kwarg_absent() -> None:
    specs = {e.spec_ref: e for e in [_magic_class("pkg.mod", "Cart", test=None)]}
    assert synthesize_auto_class_test_entries(
        specs, default_on=False, tests_package="tests", generated_dir="__generated__"
    ) == {}
    assert synthesize_auto_class_test_entries(
        specs, default_on=True, tests_package="tests", generated_dir="__generated__"
    ) != {}


def test_kwarg_false_overrides_default_on() -> None:
    specs = {e.spec_ref: e for e in [_magic_class("pkg.mod", "Cart", test=False)]}
    assert synthesize_auto_class_test_entries(
        specs, default_on=True, tests_package="tests", generated_dir="__generated__"
    ) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_auto_class_tests.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement** (append to `src/jaunt/module_contract.py`)

```python
def synthesize_auto_class_test_entries(
    magic_specs: dict[SpecRef, SpecEntry],
    *,
    default_on: bool,
    tests_package: str,
    generated_dir: str,
) -> dict[str, list[SpecEntry]]:
    out: dict[str, list[SpecEntry]] = {}
    for ref, entry in sorted(magic_specs.items(), key=lambda kv: str(kv[0])):
        if entry.class_name is not None or "." in entry.qualname:
            continue
        if not isinstance(entry.obj, type):
            continue
        flag = entry.decorator_kwargs.get("test")
        enabled = bool(flag) if flag is not None else default_on
        if not enabled:
            continue
        auto_module = f"{tests_package}.__auto__.{entry.module}"
        test_name = f"test_{entry.qualname.lower()}_baseline"
        test_ref = normalize_spec_ref(f"{auto_module}:{test_name}")
        synth = SpecEntry(
            kind="test",
            spec_ref=test_ref,
            module=auto_module,
            qualname=test_name,
            source_file=entry.source_file,
            obj=object(),
            decorator_kwargs={"targets": (ref,), "public_api_only": True},
        )
        out.setdefault(auto_module, []).append(synth)
    return out
```

In `tester.py`, add output-path handling for synthetic auto modules. In `_resolve_test_output_path`, when `source_file` cannot be matched to a test root (the auto entries' source_file is under `src/`), fall back to a deterministic auto path. Add a guard at the start of `_resolve_test_output_path`-callers, or extend it:

```python
def _auto_test_output_path(
    *, project_dir: Path, module_name: str, generated_dir: str, tests_package: str
) -> Path:
    # tests.__auto__.pkg.mod -> <project>/<tests_package>/<generated_dir>/auto/pkg/mod.py
    suffix = module_name.split(".__auto__.", 1)[1]
    rel = Path(tests_package) / generated_dir / "auto" / Path(*suffix.split("."))
    return (project_dir / rel).with_suffix(".py")
```

Wire it: in `gen_one`, `detect_stale_test_modules`, and `_collect_*` helpers, when `module_name` contains `".__auto__."`, use `_auto_test_output_path(...)` instead of `_resolve_test_output_path(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_auto_class_tests.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix src/jaunt/module_contract.py src/jaunt/tester.py tests/test_auto_class_tests.py
uv run ty check
git add src/jaunt/module_contract.py src/jaunt/tester.py tests/test_auto_class_tests.py
git commit -m "feat: synthesize virtual baseline test specs for @magic(test=True) classes"
```

---

### Task B4: Feed hybrid summary + generated-API digest into test generation & staleness

**Files:**
- Modify: `src/jaunt/cli.py` (`cmd_test` ~line 1075-1234): include synthesized auto entries in `module_specs`; build `dependency_apis` for class targets from the GENERATED module via `build_generated_class_api_summary`; add generated-API digest to staleness inputs.
- Modify: `src/jaunt/tester.py` (`detect_stale_test_modules`): accept an optional `target_api_digests: dict[str, str]` and include it in the staleness comparison (mirror `module_context_digests`).
- Test: extend `tests/test_auto_class_tests.py` with a staleness assertion using a fake generated module on disk.

**Interfaces:**
- Consumes: `build_generated_class_api_summary`, `generated_public_api_digest` (B1); `synthesize_auto_class_test_entries` (B3).
- Produces: test modules targeting a `@magic` class receive the generated public surface; changing that surface marks them stale.

- [ ] **Step 1: Write the failing test** — a focused unit test on `detect_stale_test_modules` honoring a `target_api_digests` change:

```python
# append to tests/test_auto_class_tests.py
def test_target_api_digest_change_marks_stale(tmp_path, monkeypatch) -> None:
    # Build a generated test module on disk with a header, then show that
    # changing the recorded target-API digest flips it to stale.
    # Use tester.detect_stale_test_modules with target_api_digests param.
    ...
```

> Flesh out using `tests/test_tester*.py` patterns (write a header via `jaunt.header.format_header`, then assert staleness toggles when `target_api_digests` differs from the embedded value). If the header has no slot for target-API digest, reuse `module_context_digest` by folding the target-API digest into the test module's `module_context_digest` instead (simpler — see note).

- [ ] **Step 2: Run test to verify it fails / decide approach**

Run: `uv run pytest tests/test_auto_class_tests.py -k target_api -v`
Expected: FAIL.

**Simplest viable wiring (recommended):** rather than a new header field, fold the target class's `generated_public_api_digest` into the test module's `module_context_digest` (already tracked end-to-end by `detect_stale_test_modules` via `module_context_digests`). In `cli.py`'s test-context-digest loop (cli.py:1159-1164), for each test module, append the generated-API digests of its class targets to the contract digest input.

- [ ] **Step 3: Implement**

- In `cli.py`, after computing `targeted_test_entries`, also compute `auto_entries = synthesize_auto_class_test_entries(build_magic_specs, default_on=cfg.test.auto_class_tests, tests_package=<tests pkg>, generated_dir=cfg.paths.generated_dir)` and merge into `module_specs` and `specs`/`spec_graph`/`module_dag`.
- Build `magic_dependency_apis` for class targets from the generated module on disk: for a magic class spec, read its generated file (`paths.spec_module_to_generated_module` → file), and set `magic_dependency_apis[ref] = build_generated_class_api_summary(generated_source, ClassName, spec_docstring=<spec class docstring>, public_api_only=<target's policy>).to_prompt_block()`. Fall back to the existing `build_dependency_api_block(entry)` if the generated file is absent.
- For each test module, fold `generated_public_api_digest(generated_source, ClassName)` for its class targets into the `build_module_contract(...).digest` input used for `test_module_context_digests` (concatenate and re-hash, or pass through a small helper).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_auto_class_tests.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix src/jaunt/cli.py src/jaunt/tester.py tests/test_auto_class_tests.py
uv run ty check
git add src/jaunt/cli.py src/jaunt/tester.py tests/test_auto_class_tests.py
git commit -m "feat: source class test API from generated impl; track generated-API staleness"
```

---

### Task B5: Class-aware test prompt + white-box fragility warning

**Files:**
- Modify: `src/jaunt/prompts/test_module.md`, `src/jaunt/prompts/test_system.md`
- Modify: `src/jaunt/tester.py` (emit warning when a class target has `public_api_only=False`)
- Test: `tests/test_auto_class_tests.py` (assert prompt files contain class guidance; assert warning emitted)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_auto_class_tests.py
from pathlib import Path

import jaunt


def test_test_prompt_has_class_guidance() -> None:
    root = Path(jaunt.__file__).parent / "prompts"
    text = (root / "test_module.md").read_text()
    assert "stateful" in text.lower()
    assert "isinstance" in text.lower() or "abc" in text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_auto_class_tests.py::test_test_prompt_has_class_guidance -v`
Expected: FAIL.

- [ ] **Step 3: Implement** — add to `prompts/test_module.md` (after "Test quality:"):

```markdown
- When a target is a class, test it holistically: construct it, drive realistic
  sequences of method calls (stateful scenarios), and assert invariants across
  calls — not just isolated per-method results.
- Verify the class satisfies its declared base classes / ABCs (e.g. `isinstance`,
  instantiability) and that overrides behave consistently with the base contract.
- Do not re-test unchanged inherited methods; focus on this class's own behavior.
```

Add a parallel line to `test_system.md`. In `tester.py` `gen_one`, when an entry's `public_api_only` is `False` and any target is a class spec, log a warning (reuse the `_phase`/progress channel or `diagnostics`): "white-box tests on generated internals are fragile across regeneration."

- [ ] **Step 4: Run tests, commit**

```bash
uv run pytest tests/test_auto_class_tests.py -v
uv run ruff check --fix src/jaunt/prompts/test_module.md src/jaunt/prompts/test_system.md src/jaunt/tester.py
uv run ty check
git add src/jaunt/prompts/test_module.md src/jaunt/prompts/test_system.md src/jaunt/tester.py tests/test_auto_class_tests.py
git commit -m "feat: class-aware test prompt and white-box fragility warning"
```

---

### Task B6: Implicit-test example + docs + full suite (Part B done)

**Files:**
- Modify: `examples/06_whole_class/src/whole_class_demo/specs.py` (add a `@jaunt.magic(test=True)` class), `examples/06_whole_class/jaunt.toml` (optionally `[test] auto_class_tests`)
- Modify: `CLAUDE.md` (auto-testing paragraph)

- [ ] **Step 1: Add an implicit-test class to the example**

```python
@jaunt.magic(test=True)
class TempStats:
    """Rolling temperature stats. record(temp) stores a reading;
    mean() returns the average; max() returns the highest; reset() clears all."""
```

- [ ] **Step 2: Generate + test the example** (requires API key)

Run: `cd examples/06_whole_class && uv run --project ../.. jaunt test`
Expected: a baseline test module is generated under `tests/__generated__/auto/whole_class_demo/specs.py` and pytest runs it.

- [ ] **Step 3: Update `CLAUDE.md`** with the auto-testing paragraph (explicit `@jaunt.test(targets=Cls)` + `@jaunt.magic(test=True)` + `[test] auto_class_tests`).

- [ ] **Step 4: Run the full suite**

```bash
uv run pytest
uv run ruff check .
uv run ty check
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add examples/06_whole_class CLAUDE.md
git commit -m "docs: implicit auto-testing example and guide"
```

---

## Self-Review notes (for the implementer)

- **Spec coverage:** §1 authoring/modes → A1; `@jaunt.preserve` → A1 (detect) + A2 (runtime) + A5 (validation) + A7 (prompt strips it); §2 contract/inheritance → A4/A7; §3 single-shot generation → A7; §4 validation → A5/A7; §5 incremental/deps/runtime → A6/A7; §6.1 hybrid API → B1/B4; §6.2 explicit improvements → B4/B5; §6.3 implicit → A3/B2/B3/B4/B6; deliverables/tests/risks → covered across tasks.
- **Type consistency:** `MemberSplit`, `BaseContract`, `SpecApiSummary`, `validate_build_class_source` kwargs, and `synthesize_auto_class_test_entries` return shape are used identically wherever referenced.
- **Known soft spots to verify during execution (not placeholders — verify against live code):** the exact insertion points in `builder.py` `_component_payload`/`_validate_module_candidate`; the `ModuleSpecContext` field addition in `generate/base.py` and its rendering in `build_module.md`; the cli.py merge of auto entries into `module_specs`/`specs`/`spec_graph`/`module_dag`; and the `detect_stale_test_modules` digest folding. Each has a test that pins the intended behavior; if the wiring differs, keep the test and adjust the wiring.
```
