# Class Interface Guideposts + Inheritance-Aware Whole-Class Magic — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-03-class-interface-guideposts-design.md` — read it before starting any task.

**Goal:** Three-tier method vocabulary on whole-class `@magic` specs (preserved / sealed via inner `@jaunt.magic` / guidepost default) plus inheritance-aware generation: always-on structural base edges, artifact-derived inherited-API context for cross-module bases, base-API staleness propagation with a refreeze guard, and always-on composition guidance.

**Architecture:** Inner `@jaunt.magic` on a method of a whole-class spec is *absorbed* at class-decoration time (registry-keyed, originals restored) and recorded as `sealed_members`; the AST layer (`class_analysis.py`) classifies the same three tiers independently for build/digest/validation. Base-class spec refs become an ungated `base_deps` edge source. Cross-module spec'd bases contribute a rendered inherited-API block (signatures + docstrings from the generated artifact on disk) that joins the module context digest — as does the whole-class contract block — with empty-block guards so function-only projects keep byte-identical digests.

**Tech Stack:** Python 3.12+, `ast`, `inspect`, pytest, ruff (line-length 100, rules E/F/I/UP/B), `ty`, `uv`.

## Global Constraints

- Python 3.12+; ruff line-length 100; `from __future__ import annotations` at the top of every module (match existing files).
- After each task run: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format .`, `uv run ty check` — all must pass before committing.
- Tests mock the generator backend; never require API keys or the `codex` binary.
- **Digest byte-compatibility is a hard requirement:** any change to `digest.py` / `_build_context_digest` must leave marker-free, function-only projects with byte-identical digests. New digest inputs are folded **only when non-empty** (never add an unconditional `h.update(b"\x00")`).
- Errors from the new tier rules are config/discovery errors (raise `JauntError`, surfacing as exit 2) — never a model-call-time failure.
- Standalone method-level `@magic` on an *undecorated* class keeps its exact current behavior.
- Conventional commits (`feat:`, `test:`, `refactor:`, `docs:`); commit after every task.
- Do not edit anything under `__generated__/`.

## File Structure

- **Modify** `src/jaunt/registry.py` — `unregister_magic`; `SpecEntry.sealed_members` + `SpecEntry.base_deps`.
- **Modify** `src/jaunt/class_analysis.py` — public `is_magic_decorator`, 3-way `MemberSplit` (sealed ⊆ stubs), tier error checks, `canonical_signature`, scaffold strips inner magic, tiered `render_whole_class_contract` + composition paragraph + inherited-API section.
- **Modify** `src/jaunt/runtime.py` — absorption in the whole-class branch; `base_deps` recording (replaces the `auto_deps` merge).
- **Modify** `src/jaunt/deps.py` — ungated `base_deps` edges in `build_spec_graph`.
- **Modify** `src/jaunt/digest.py` — `"sealed"` tag in `_normalized_members` (only when present).
- **Modify** `src/jaunt/validation.py` — `sealed_signatures` enforcement in `validate_build_class_source`.
- **Modify** `src/jaunt/builder.py` — `_whole_class_context` helper (base contract + inherited API + contract block + `base_api_digest`), context-digest wiring, guard message rework, refreeze guard, header field.
- **Modify** `src/jaunt/status_core.py`, `src/jaunt/cli.py` — use the shared helper so status/build digests agree.
- **Tests:** `tests/test_registry.py` (or additions), `tests/test_class_analysis.py`, `tests/test_magic_decorator.py`, `tests/test_builder_methods.py`, `tests/test_deps.py`, `tests/test_digest.py`, `tests/test_validation_class.py`, `tests/test_builder_whole_class.py`, new `tests/test_inheritance_e2e.py`.
- **Docs:** `CLAUDE.md`, instructions primer template, `docs-site/content/docs/writing-specs/magic.mdx`, `writing-specs/dependencies.mdx`, `reference/change-detection.mdx`, bundled jaunt skill.

---

### Task 1: Registry — `unregister_magic` + new `SpecEntry` fields

**Files:**
- Modify: `src/jaunt/registry.py`
- Test: `tests/test_registry.py` (create if absent; else append)

**Interfaces:**
- Produces: `unregister_magic(spec_ref: SpecRef) -> SpecEntry | None`;
  `SpecEntry.sealed_members: tuple[str, ...] = ()`;
  `SpecEntry.base_deps: tuple[SpecRef, ...] = ()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_registry.py (append; create with standard imports if missing)
from __future__ import annotations

from jaunt.registry import (
    SpecEntry,
    clear_registries,
    get_magic_registry,
    register_magic,
    unregister_magic,
)
from jaunt.spec_ref import SpecRef


def _entry(ref: str, *, class_name: str | None = None) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=SpecRef(ref),
        module=ref.split(":", 1)[0],
        qualname=ref.split(":", 1)[1],
        source_file="x.py",
        obj=object(),
        decorator_kwargs={},
        class_name=class_name,
    )


def test_unregister_magic_removes_and_returns_entry() -> None:
    clear_registries()
    entry = _entry("m:C.f", class_name="C")
    register_magic(entry)
    assert unregister_magic(SpecRef("m:C.f")) is entry
    assert SpecRef("m:C.f") not in get_magic_registry()
    assert unregister_magic(SpecRef("m:C.f")) is None


def test_spec_entry_new_fields_default_empty() -> None:
    entry = _entry("m:C")
    assert entry.sealed_members == ()
    assert entry.base_deps == ()
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_registry.py -q` → ImportError (`unregister_magic`).

- [ ] **Step 3: Implement**

In `SpecEntry` (after `auto_deps`):

```python
    sealed_members: tuple[str, ...] = ()
    base_deps: tuple[SpecRef, ...] = ()
```

After `register_magic`:

```python
def unregister_magic(spec_ref: SpecRef) -> SpecEntry | None:
    """Remove and return a magic spec entry (``None`` if absent).

    Used by whole-class absorption: inner ``@magic`` method specs are folded
    into their class's spec at class-decoration time.
    """

    return _MAGIC_REGISTRY.pop(spec_ref, None)
```

- [ ] **Step 4: Run tests, lint, typecheck** — all green.
- [ ] **Step 5: Commit** — `feat(registry): unregister_magic + sealed_members/base_deps fields`

---

### Task 2: AST layer — sealed tier in `class_analysis.py`

**Files:**
- Modify: `src/jaunt/class_analysis.py`
- Test: `tests/test_class_analysis.py`

**Interfaces:**
- Produces: `is_magic_decorator(dec: ast.expr) -> bool` (public rename of `_is_magic_decorator`; keep a `_is_magic_decorator = is_magic_decorator` alias so existing importers don't break);
  `MemberSplit` gains `sealed: tuple[str, ...]` — **sealed is a subset of `stubs`** (both are "to implement"; guideposts = `stubs` minus `sealed`), so existing consumers of `stubs` (scaffold sentinels, validation existence checks, digest) keep working unchanged;
  `split_class_members` raises `JauntError` on tier violations;
  `build_class_scaffold` strips inner `@jaunt.magic` from method decorator lists.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_class_analysis.py (append)
import pytest

from jaunt.errors import JauntError  # match the import used by src/jaunt/runtime.py
from jaunt.class_analysis import build_class_scaffold, is_magic_decorator


def test_split_sealed_subset_of_stubs() -> None:
    cls = _cls(
        "class C:\n"
        "    @jaunt.magic\n"
        "    def locked(self, x: int) -> int: ...\n"
        "    def sketch(self): ...\n"
        "    def real(self):\n        return 1\n"
    )
    split = split_class_members(cls)
    assert split.sealed == ("locked",)
    assert set(split.sealed) <= set(split.stubs)
    assert split.stubs == ("locked", "sketch")
    assert split.preserved == ("real",)


def test_magic_plus_preserve_raises() -> None:
    cls = _cls(
        "class C:\n"
        "    @jaunt.magic\n"
        "    @jaunt.preserve\n"
        "    def m(self): ...\n"
    )
    with pytest.raises(JauntError, match="preserve"):
        split_class_members(cls)


def test_magic_on_non_stub_body_raises() -> None:
    cls = _cls(
        "class C:\n"
        "    @jaunt.magic\n"
        "    def m(self):\n        return 1\n"
    )
    with pytest.raises(JauntError, match="preserve"):
        split_class_members(cls)


def test_magic_on_property_raises() -> None:
    cls = _cls(
        "class C:\n"
        "    @property\n"
        "    @jaunt.magic\n"
        "    def m(self) -> int: ...\n"
    )
    with pytest.raises(JauntError, match="property"):
        split_class_members(cls)


def test_classify_counts_sealed_as_stubs() -> None:
    cls = _cls(
        "class C:\n"
        "    @jaunt.magic\n"
        "    def m(self): ...\n"
    )
    assert classify_class_mode(cls) == "stubs"


def test_scaffold_strips_inner_magic() -> None:
    seg = (
        "@jaunt.magic()\n"
        "class C:\n"
        '    """doc"""\n'
        "    @jaunt.magic\n"
        "    def locked(self, x: int) -> int: ...\n"
    )
    out = build_class_scaffold(seg)
    assert "@jaunt.magic" not in out
    assert "def locked(self, x: int) -> int:" in out
    assert "# jaunt:implement" in out
```

(Reuse the existing `_cls` / `split_class_members` / `classify_class_mode` imports already in this test file.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_class_analysis.py -q`.

- [ ] **Step 3: Implement**

1. Rename `_is_magic_decorator` → `is_magic_decorator`; add `_is_magic_decorator = is_magic_decorator` alias. Move it **above** `split_class_members`. Note: `src/jaunt/builder.py` imports `_is_magic_decorator` from this module — the alias keeps that working.
2. Add a property detector next to it:

```python
def _is_property_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id == "property"
    if isinstance(target, ast.Attribute):
        return target.attr in {"setter", "getter", "deleter"}
    return False
```

3. `MemberSplit` gains `sealed: tuple[str, ...]` (after `stubs`). Rewrite `split_class_members`:

```python
def split_class_members(class_node: ast.ClassDef) -> MemberSplit:
    from jaunt.errors import JauntError

    stubs: list[str] = []
    sealed: list[str] = []
    preserved: list[str] = []
    preserve_marked: list[str] = []
    for fn in _iter_methods(class_node):
        marked = any(is_preserve_decorator(d) for d in fn.decorator_list)
        magic_marked = any(is_magic_decorator(d) for d in fn.decorator_list)
        if magic_marked:
            if marked:
                raise JauntError(
                    f"{class_node.name}.{fn.name}: @jaunt.magic and @jaunt.preserve are "
                    "contradictory tiers; use exactly one."
                )
            if any(_is_property_decorator(d) for d in fn.decorator_list):
                raise JauntError(
                    f"{class_node.name}.{fn.name}: @property cannot be sealed with inner "
                    "@jaunt.magic (v1); leave it as a guidepost stub or hand-write it "
                    "with @jaunt.preserve."
                )
            if not is_stub_body(fn):
                raise JauntError(
                    f"{class_node.name}.{fn.name}: inner @jaunt.magic on a hand-written "
                    "body; use @jaunt.preserve to keep it, or reduce it to a stub for "
                    "Jaunt to implement."
                )
            sealed.append(fn.name)
            stubs.append(fn.name)
        elif marked:
            preserve_marked.append(fn.name)
            preserved.append(fn.name)
        elif is_stub_body(fn):
            stubs.append(fn.name)
        else:
            preserved.append(fn.name)
    return MemberSplit(
        stubs=tuple(sorted(stubs)),
        sealed=tuple(sorted(sealed)),
        preserved=tuple(sorted(preserved)),
        preserve_marked=tuple(sorted(preserve_marked)),
    )
```

4. In `build_class_scaffold`'s preserved-clone loop and in `_stub_node_with_sentinel`'s clone, strip inner magic too. Simplest: in `_stub_node_with_sentinel`, after the clone assert add
   `clone.decorator_list = [d for d in clone.decorator_list if not is_magic_decorator(d)]`,
   and in the preserved loop extend the existing filter:
   `clone.decorator_list = [d for d in clone.decorator_list if not (is_preserve_decorator(d) or is_magic_decorator(d))]`.

Check every `MemberSplit(` construction site in `src/` and `tests/` (grep) and add `sealed=()` where needed.

- [ ] **Step 4: Run full suite, lint, typecheck** — `uv run pytest -q` (existing digest/scaffold tests must stay green — sealed ⊆ stubs keeps them).
- [ ] **Step 5: Commit** — `feat(class-analysis): sealed tier — 3-way member split + tier violation errors`

---

### Task 3: Runtime absorption + `base_deps` recording

**Files:**
- Modify: `src/jaunt/runtime.py`
- Test: `tests/test_magic_decorator.py`

**Interfaces:**
- Consumes: `unregister_magic`, `SpecEntry.sealed_members`, `SpecEntry.base_deps` (Task 1).
- Produces: whole-class `SpecEntry` with `sealed_members` populated and `base_deps` recorded; **`auto_deps` no longer contains base refs** (the merge at `runtime.py:236-247` is removed).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_magic_decorator.py (append)
def test_inner_magic_absorbed_into_whole_class_spec(clean_registry, tmp_path, monkeypatch):
    # Follow this file's existing fixture pattern for creating an importable module.
    src = textwrap.dedent(
        """
        import jaunt

        @jaunt.magic()
        class Engine:
            '''An engine.'''

            @jaunt.magic
            def start(self, power: int) -> bool: ...

            def helper(self): ...
        """
    )
    mod = _import_module_from_source(tmp_path, "absorb_mod", src)  # existing helper pattern
    reg = get_magic_registry()
    refs = [str(r) for r in reg]
    assert refs == ["absorb_mod:Engine"]  # no phantom method spec
    entry = reg[SpecRef("absorb_mod:Engine")]
    assert entry.sealed_members == ("start",)


def test_inner_magic_original_function_restored(clean_registry, tmp_path):
    # After absorption the registered class object's member is the original
    # function (stub), not a jaunt method wrapper.
    ...  # same module as above; then:
    entry = get_magic_registry()[SpecRef("absorb_mod:Engine")]
    member = entry.obj.__dict__["start"]
    assert not hasattr(member, "__wrapped__")
    assert member.__name__ == "start"


def test_inner_magic_classmethod_descriptor_reconstructed(clean_registry, tmp_path):
    src = textwrap.dedent(
        """
        import jaunt

        @jaunt.magic()
        class Engine:
            '''doc'''

            @classmethod
            @jaunt.magic
            def make(cls) -> "Engine": ...
        """
    )
    ...
    entry = get_magic_registry()[SpecRef("absorb_mod2:Engine")]
    assert isinstance(entry.obj.__dict__["make"], classmethod)
    assert entry.sealed_members == ("make",)


def test_inner_magic_with_kwargs_rejected(clean_registry, tmp_path):
    src = textwrap.dedent(
        """
        import jaunt

        @jaunt.magic()
        class Engine:
            '''doc'''

            @jaunt.magic(deps=[])
            def start(self) -> None: ...
        """
    )
    with pytest.raises(JauntError, match="kwargs"):
        _import_module_from_source(tmp_path, "absorb_bad", src)


def test_standalone_method_magic_unchanged(clean_registry, tmp_path):
    src = textwrap.dedent(
        """
        import jaunt

        class Plain:
            @jaunt.magic()
            def go(self) -> int: ...
        """
    )
    ...
    refs = [str(r) for r in get_magic_registry()]
    assert refs == ["plain_mod:Plain.go"]


def test_whole_class_base_deps_recorded_not_in_auto_deps(clean_registry, tmp_path):
    src = textwrap.dedent(
        """
        import jaunt

        @jaunt.magic()
        class Base:
            '''base'''
            def run(self) -> None: ...

        @jaunt.magic()
        class Child(Base):
            '''child'''
            def go(self) -> None: ...
        """
    )
    ...
    child = get_magic_registry()[SpecRef("basemod:Child")]
    assert any(str(r).endswith(":Base") for r in child.base_deps)
    assert not any(str(r).endswith(":Base") for r in child.auto_deps)
```

Flesh the `...` bodies out following the module-import helper already used in this test file (several tests there import a temp module via `importlib`; reuse that fixture/helper verbatim). Note: `Base` is substituted at import time; if unbuilt, `@magic` returns a placeholder class — `Child(Base)` still works because the placeholder is a real `type`. If placeholder inheritance trips `__new__`, define the base with a built `__generated__` fixture the way `tests/test_magic_decorator.py`'s substitution tests do.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement in `runtime.py`**

Add near `_make_method_wrapper`:

```python
def _absorb_method_specs(cls_obj: type, *, module: str, class_name: str) -> tuple[str, ...]:
    """Fold inner ``@magic`` method specs into their whole-class spec.

    Returns the sorted member names that become the class's sealed tier.
    Restores original functions (recovered via ``__wrapped__``) onto the class
    so the registered runtime object matches the source AST.
    """

    from jaunt.registry import get_magic_registry, unregister_magic

    absorbed = [
        e
        for e in list(get_magic_registry().values())
        if e.kind == "magic" and e.module == module and e.class_name == class_name
    ]
    names: list[str] = []
    for entry in absorbed:
        unregister_magic(entry.spec_ref)
        member_name = entry.qualname.rsplit(".", 1)[-1]
        if entry.decorator_kwargs:
            raise JauntError(
                f"{class_name}.{member_name}: inner @jaunt.magic inside a whole-class "
                "spec takes no kwargs (v1); move deps=/test=/prompt= to the class-level "
                "@jaunt.magic."
            )
        slot = cls_obj.__dict__.get(member_name)
        if isinstance(slot, property):
            raise JauntError(
                f"{class_name}.{member_name}: @property cannot be sealed with inner "
                "@jaunt.magic (v1)."
            )
        descriptor_type: type | None = None
        wrapper = slot
        if isinstance(slot, (classmethod, staticmethod)):
            descriptor_type = type(slot)
            wrapper = slot.__func__
        original = getattr(wrapper, "__wrapped__", None)
        if original is None:
            continue  # wrapper shape unknown; leave the slot alone (AST layer still seals)
        if getattr(wrapper, "__isabstractmethod__", False):
            original.__isabstractmethod__ = True  # type: ignore[attr-defined]
        restored: object = original if descriptor_type is None else descriptor_type(original)
        setattr(cls_obj, member_name, restored)
        names.append(member_name)
    return tuple(sorted(names))
```

In `_decorate`, at the **top of the whole-class handling** — i.e. inside a new
`if isinstance(obj, type) and class_name is None:` placed *before*
`analyze_magic_decorators` runs (line ~228), so analysis/registration see the restored
class:

```python
        sealed_members: tuple[str, ...] = ()
        base_deps: tuple[SpecRef, ...] = ()
        if isinstance(obj, type) and class_name is None:
            sealed_members = _absorb_method_specs(obj, module=module, class_name=qualname)
```

Replace the existing base-ref merge block (`runtime.py:236-247`) with:

```python
        if isinstance(obj, type) and class_name is None:
            from jaunt.class_analysis import resolve_base_contract

            refs: list[SpecRef] = []
            for ref_str in resolve_base_contract(obj).project_base_refs:
                try:
                    refs.append(normalize_spec_ref(ref_str))
                except Exception:
                    continue
            base_deps = tuple(sorted(set(refs), key=str))
```

and pass both to `SpecEntry(...)`: `sealed_members=sealed_members, base_deps=base_deps,`
with `auto_deps=analysis.auto_deps` (no merge). The non-stub-body runtime check is
**not** duplicated here — the AST layer (Task 2) raises it deterministically at
digest/build time; runtime absorption only enforces kwargs and property (cheap,
source-independent).

- [ ] **Step 4: Run full suite, lint, typecheck.** `tests/test_deps.py::…auto_deps…` tests still pass because they construct entries directly.
- [ ] **Step 5: Commit** — `feat(runtime): absorb inner @magic method specs into whole-class specs; record base_deps`

---

### Task 4: Rework the mixed-magic guard

**Files:**
- Modify: `src/jaunt/builder.py:1229-1236` (`_build_expected_names`)
- Test: `tests/test_builder_methods.py` (existing conflict test near line 260)

**Interfaces:** message change only; return shape unchanged.

- [ ] **Step 1: Update the test.** Find the test asserting `"Use one or the other"`. It constructs entries directly (a real import can no longer produce the state after Task 3). Keep the hand-constructed entries; change the expected message match to `"should have been absorbed"`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** — replace the error string:

```python
        return expected, [
            f"Conflicting @magic: class(es) {names} have both whole-class @magic and "
            "per-method @magic registry entries. Inner @magic methods of a whole-class "
            "spec should have been absorbed at import time; this indicates a "
            "registration bug (or a hand-constructed registry)."
        ]
```

- [ ] **Step 4: Run suite, lint, typecheck.**
- [ ] **Step 5: Commit** — `refactor(builder): mixed-magic guard is now a defense-in-depth invariant`

---

### Task 5: Ungated base edges in `build_spec_graph`

**Files:**
- Modify: `src/jaunt/deps.py` (`build_spec_graph`, after the explicit-deps loop, before the infer gate)
- Test: `tests/test_deps.py`

**Interfaces:**
- Consumes: `SpecEntry.base_deps` (Task 1).
- Produces: base edges present regardless of `infer_default` / `infer_deps` overrides.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_deps.py (append)
def test_base_deps_edge_exists_with_inference_off() -> None:
    a = _entry("pkg.base:A")           # reuse this file's entry-construction helper
    b = _entry("pkg.child:B", base_deps=(SpecRef("pkg.base:A"),))
    specs = {a.spec_ref: a, b.spec_ref: b}
    graph = build_spec_graph(specs, infer_default=False)
    assert SpecRef("pkg.base:A") in graph[SpecRef("pkg.child:B")]


def test_base_deps_ignore_unknown_and_self() -> None:
    b = _entry("pkg.child:B", base_deps=(SpecRef("pkg.child:B"), SpecRef("ext:Nope")))
    graph = build_spec_graph({b.spec_ref: b}, infer_default=False)
    assert graph[SpecRef("pkg.child:B")] == set()


def test_base_deps_cycle_detected() -> None:
    a = _entry("pkg.a:A", base_deps=(SpecRef("pkg.b:B"),))
    b = _entry("pkg.b:B", base_deps=(SpecRef("pkg.a:A"),))
    graph = build_spec_graph({a.spec_ref: a, b.spec_ref: b}, infer_default=False)
    assert find_cycles(graph)
```

(If `_entry` doesn't take `base_deps`, extend the helper with a passthrough kwarg.)

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** — in `build_spec_graph`, directly after the explicit-deps loop (`deps.py:252-258`):

```python
        # Structural base-class edges: never gated by inference — inheritance
        # is a fact of the class header, not a guess (spec 2026-07-03).
        for dep_ref in entry.base_deps:
            if dep_ref != spec_ref and dep_ref in specs:
                deps_out.add(dep_ref)
```

Also update `build_spec_graph`'s docstring to document the third edge source.

- [ ] **Step 4: Run suite, lint, typecheck.**
- [ ] **Step 5: Commit** — `feat(deps): always-on structural base-class edges`

---

### Task 6: Sealed tag in the contract digest (byte-compatible)

**Files:**
- Modify: `src/jaunt/digest.py` (`_normalized_members`, ~line 304)
- Test: `tests/test_digest.py`

**Interfaces:** none new; behavior: sealed methods add `"sealed": "1"` to their member record — key absent otherwise.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_digest.py (append)
def test_sealed_marker_changes_class_digest() -> None:
    plain = "class C:\n    def m(self, x: int) -> int: ...\n"
    sealed = "class C:\n    @jaunt.magic\n    def m(self, x: int) -> int: ...\n"
    assert _members_json(plain) != _members_json(sealed)   # helper below


def test_marker_free_members_json_has_no_sealed_key() -> None:
    plain = "class C:\n    def m(self, x: int) -> int: ...\n"
    assert '"sealed"' not in _members_json(plain)


def _members_json(src: str) -> str:
    import ast
    from jaunt.digest import _normalized_members

    node = ast.parse(src).body[0]
    assert isinstance(node, ast.ClassDef)
    return _normalized_members(node)
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** — in `_normalized_members`, after `record = {...}` (line ~323):

```python
        if name in split.sealed:
            record["sealed"] = "1"
```

(`split` is already computed at the top of the function; the jaunt-decorator filter in
`_decorator_meta`/`class_decorators` already excludes `@jaunt.magic` from decorator
lists, so no other digest input moves.)

- [ ] **Step 4: Run suite, lint, typecheck** — every pre-existing digest test must pass untouched (that *is* the byte-compat proof).
- [ ] **Step 5: Commit** — `feat(digest): sealed tier participates in the contract digest (byte-compatible)`

---

### Task 7: Sealed-signature validation

**Files:**
- Modify: `src/jaunt/class_analysis.py` (`canonical_signature`), `src/jaunt/validation.py` (`validate_build_class_source`), `src/jaunt/builder.py` (`_class_validation_inputs`)
- Test: `tests/test_validation_class.py`

**Interfaces:**
- Produces: `canonical_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str` in `class_analysis.py` (the one comparator; validation is its only equality consumer — `digest._function_signature` intentionally keeps its existing rendering for byte-compat);
  `validate_build_class_source(..., sealed_signatures: dict[str, str] | None = None)` — values are `canonical_signature` outputs;
  `_class_validation_inputs` returns a `"sealed_signatures"` key.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_validation_class.py (append)
from jaunt.class_analysis import canonical_signature


def _sig(src: str) -> str:
    import ast

    fn = ast.parse(src).body[0]
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    return canonical_signature(fn)


SEALED = {"m": _sig("def m(self, x: int, *, retries: int = 3) -> bool: ...")}


def test_sealed_exact_match_passes() -> None:
    out = "class C:\n    def m(self, x: int, *, retries: int = 3) -> bool:\n        return True\n"
    errs = validate_build_class_source(
        out, class_name="C", stub_methods=["m"], preserved_segments={},
        declared_bases=[], class_decorators=[], required_abstractmethods=[],
        spec_docstring="", sealed_signatures=SEALED,
    )
    assert errs == []


@pytest.mark.parametrize(
    "bad",
    [
        "def m(self, x: int, *, tries: int = 3) -> bool:",      # renamed param
        "def m(self, x: int, *, retries: int = 5) -> bool:",    # changed default
        "def m(self, x: int, *, retries: int = 3) -> int:",     # changed return
        "def m(self, x: int, extra: str, *, retries: int = 3) -> bool:",  # added param
    ],
)
def test_sealed_drift_is_error(bad: str) -> None:
    out = f"class C:\n    {bad}\n        return True\n"
    errs = validate_build_class_source(
        out, class_name="C", stub_methods=["m"], preserved_segments={},
        declared_bases=[], class_decorators=[], required_abstractmethods=[],
        spec_docstring="", sealed_signatures=SEALED,
    )
    assert any("sealed" in e for e in errs)


def test_guidepost_drift_stays_warn_only() -> None:
    # No sealed_signatures entry for the method ⇒ drift produces no error here
    # (class_build_warnings still warns on dropped params — unchanged).
    out = "class C:\n    def m(self, renamed: int) -> bool:\n        return True\n"
    errs = validate_build_class_source(
        out, class_name="C", stub_methods=["m"], preserved_segments={},
        declared_bases=[], class_decorators=[], required_abstractmethods=[],
        spec_docstring="", sealed_signatures={},
    )
    assert errs == []
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement**

`class_analysis.py`:

```python
def canonical_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Formatting-insensitive signature identity for sealed-method comparison."""

    a = node.args
    n_pos = len(a.posonlyargs) + len(a.args)
    pos_defaults = [None] * (n_pos - len(a.defaults)) + list(a.defaults)

    def render(arg: ast.arg, default: ast.expr | None) -> list[str]:
        return [
            arg.arg,
            ast.unparse(arg.annotation) if arg.annotation else "",
            ast.unparse(default) if default is not None else "",
        ]

    parts: list[object] = ["async" if isinstance(node, ast.AsyncFunctionDef) else "def"]
    ordered = [*a.posonlyargs, *a.args]
    parts.append([render(arg, d) for arg, d in zip(ordered, pos_defaults)])
    parts.append(len(a.posonlyargs))
    parts.append(a.vararg.arg if a.vararg else "")
    parts.append([render(arg, d) for arg, d in zip(a.kwonlyargs, a.kw_defaults)])
    parts.append(a.kwarg.arg if a.kwarg else "")
    parts.append(ast.unparse(node.returns) if node.returns else "")
    import json

    return json.dumps(parts, sort_keys=False, separators=(",", ":"))
```

`validation.py` — add the parameter `sealed_signatures: dict[str, str] | None = None`
to `validate_build_class_source` and, after the unfilled-stub check:

```python
    # Sealed methods: signature is the contract — exact match required.
    if sealed_signatures:
        from jaunt.class_analysis import canonical_signature

        for name, expected_sig in sealed_signatures.items():
            node = methods.get(name)
            if node is None:
                continue  # the stub-existence check above already errored
            if canonical_signature(node) != expected_sig:
                errors.append(
                    f"{class_name}.{name}: sealed method signature drifted; implement "
                    f"exactly the declared signature (params, defaults, annotations, "
                    f"and return type) — do not rename, add, or remove parameters."
                )
```

`builder.py` `_class_validation_inputs` — add to the returned dict:

```python
        "sealed_signatures": {
            name: canonical_signature(methods[name]) for name in split.sealed
        },
```

(import `canonical_signature` alongside the existing `class_analysis` imports).

- [ ] **Step 4: Run suite, lint, typecheck.**
- [ ] **Step 5: Commit** — `feat(validation): sealed-signature drift is a hard error`

---

### Task 8: Tiered contract block + composition guidance

**Files:**
- Modify: `src/jaunt/class_analysis.py` (`render_whole_class_contract`)
- Test: `tests/test_class_analysis.py`

**Interfaces:**
- Produces: `render_whole_class_contract(*, class_segment, base_contract_block, inherited_api_block: str = "") -> str` (new optional kwarg; existing call sites keep working).

- [ ] **Step 1: Write the failing tests**

```python
def test_contract_renders_three_tiers_and_composition() -> None:
    seg = (
        "@jaunt.magic()\n"
        "class C:\n"
        '    """doc"""\n'
        "    @jaunt.magic\n"
        "    def locked(self, x: int) -> int: ...\n"
        "    def sketch(self): ...\n"
        "    @jaunt.preserve\n"
        "    def keep(self):\n        return 1\n"
    )
    out = render_whole_class_contract(class_segment=seg, base_contract_block="")
    assert "exactly" in out and "locked(self, x: int) -> int" in out   # sealed w/ signature
    assert "sketches of intent" in out and "C.sketch" in out           # guidepost
    assert "EXACTLY as written" in out and "C.keep" in out             # preserved
    assert "small, single-purpose methods" in out                      # composition, always on


def test_contract_renders_inherited_api_block() -> None:
    out = render_whole_class_contract(
        class_segment="@jaunt.magic()\nclass C:\n    def m(self): ...\n",
        base_contract_block="",
        inherited_api_block="Base.run(self) -> None\n  doc: run it",
    )
    assert "Inherited generated API" in out
    assert "Base.run(self) -> None" in out
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** — rework the middle of `render_whole_class_contract`:

```python
    guideposts = tuple(n for n in split.stubs if n not in set(split.sealed))
    methods = {n.name: n for n in _iter_methods(cls)}

    if split.sealed:
        lines.append(
            "Sealed methods — implement exactly these signatures; do not rename, add, "
            "or remove parameters or change annotations/defaults/return types:"
        )
        for name in split.sealed:
            fn = methods[name]
            prefix = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
            ret = f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
            lines.append(f"- {prefix} {name}({ast.unparse(fn.args)}){ret}")
        lines.append("")
    if guideposts:
        lines.append(
            "Guidepost methods — these signatures are sketches of intent; you may adapt "
            "them (parameters, splitting into several methods, additional public "
            "methods) as long as the documented behavior is delivered. Replace each "
            "`# jaunt:implement` body with a real implementation:"
        )
        lines.extend(f"- {cls.name}.{name}" for name in guideposts)
        lines.append("")
```

Keep the existing preserved/docstring-only/base-contract sections. After the
base-contract section add:

```python
    inherited = inherited_api_block.strip()
    if inherited:
        lines.append(
            "Inherited generated API — these base-class methods already exist; build on "
            "them instead of reimplementing:"
        )
        lines.append(inherited)
        lines.append("")
    lines.append(
        "Prefer small, single-purpose methods composed into the public interface over "
        "monolithic bodies. When a base class provides functionality — including "
        "generated methods listed in the inherited API above — build on it: call it, "
        "extend it via super(), or override it deliberately. Do not reimplement "
        "inherited behavior."
    )
    lines.append("")
```

The old flat "Replace each `# jaunt:implement`…" stubs section is replaced by the two
tier sections (sealed + guidepost). Update any existing render tests asserting the old
single-section wording.

- [ ] **Step 4: Run suite, lint, typecheck.**
- [ ] **Step 5: Commit** — `feat(prompts): tiered whole-class contract + always-on composition guidance`

---

### Task 9: Inherited-API context + digest wiring (the staleness core)

**Files:**
- Modify: `src/jaunt/builder.py` (new `_whole_class_context` helper; `build_module_context_artifacts`; `_build_context_digest`; `_component_payload` ~1640-1727; `run_build` header at ~1933; grep every other `build_module_context_artifacts(` call site — `status_core.py`, `cli.py` — and thread the helper through)
- Test: `tests/test_builder_whole_class.py`, `tests/test_digest.py` or `tests/test_builder_io.py` (follow where module-context-digest tests live; grep `module_context_digest`)

**Interfaces:**
- Produces (in `builder.py`):

```python
@dataclass(frozen=True)
class WholeClassContext:
    base_contract_block: str          # runtime-MRO walk (external + same-module bases)
    inherited_api_block: str          # artifact-derived, cross-module spec'd bases only
    whole_class_contract_block: str   # tiered render (Task 8), inherited block included
    base_api_digest: str              # sha256 of inherited_api_block; "" when no such bases

def _whole_class_context(
    entries: list[SpecEntry], *, specs: dict[SpecRef, SpecEntry],
    package_dir: Path, generated_dir: str,
) -> WholeClassContext: ...
```

- `build_module_context_artifacts(..., whole_class_contract_block: str = "", inherited_api_block: str = "")` — both fed to `_build_context_digest` **only when non-empty**.
- Header field `"base_api_digest"` written **only when non-empty**.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_builder_whole_class.py (append)
def test_inherited_api_block_rendered_from_disk_artifact(tmp_path) -> None:
    # Arrange: a built base artifact on disk for module pkg.base with class A,
    # and a Child spec in pkg.child with base_deps=("pkg.base:A",).
    _write_generated(tmp_path, "pkg.base", GENERATED_A)  # follow this file's fixtures
    ctx = _whole_class_context(
        [child_entry], specs={a_ref: a_entry, child_ref: child_entry},
        package_dir=tmp_path, generated_dir="__generated__",
    )
    assert "A.run(" in ctx.inherited_api_block          # signature from the artifact
    assert "does the run" in ctx.inherited_api_block    # docstring from the artifact
    assert ctx.base_api_digest != ""


def test_unbuilt_base_yields_sentinel(tmp_path) -> None:
    ctx = _whole_class_context(
        [child_entry], specs={a_ref: a_entry, child_ref: child_entry},
        package_dir=tmp_path, generated_dir="__generated__",
    )
    assert "unbuilt:pkg.base:A" in ctx.inherited_api_block


def test_same_module_base_excluded(tmp_path) -> None:
    # Child and A share a module ⇒ no artifact requirement, empty inherited block.
    ...
    assert ctx.inherited_api_block == ""
    assert ctx.base_api_digest == ""


def test_context_digest_unchanged_for_function_only_modules() -> None:
    # Call build_module_context_artifacts with and without the new kwargs left at
    # defaults for a function-only module: digests must be identical.
    d1 = build_module_context_artifacts(**FUNCTION_ONLY_KWARGS).digest
    d2 = build_module_context_artifacts(
        **FUNCTION_ONLY_KWARGS, whole_class_contract_block="", inherited_api_block=""
    ).digest
    assert d1 == d2


def test_context_digest_moves_with_inherited_api_block() -> None:
    base = build_module_context_artifacts(**WHOLE_CLASS_KWARGS, inherited_api_block="v1")
    changed = build_module_context_artifacts(**WHOLE_CLASS_KWARGS, inherited_api_block="v2")
    assert base.digest != changed.digest
```

(Adapt fixture names to what this test file already provides; build the `SpecEntry`
objects with `base_deps` set explicitly.)

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement**

1. `_whole_class_context` in `builder.py`:

```python
def _whole_class_context(
    entries: list[SpecEntry],
    *,
    specs: dict[SpecRef, SpecEntry],
    package_dir: Path,
    generated_dir: str,
) -> WholeClassContext:
    from jaunt.class_analysis import render_whole_class_contract, resolve_base_contract
    from jaunt.digest import extract_source_segment
    from jaunt.module_api import build_generated_class_api_summary

    whole = _whole_class_specs(entries)
    base_blocks: list[str] = []
    inherited_lines: list[str] = []
    contract_blocks: list[str] = []

    for entry in whole.values():
        base_blocks.append(resolve_base_contract(entry.obj).block)  # type: ignore[arg-type]
        entry_inherited: list[str] = []
        for dep_ref in entry.base_deps:
            dep = specs.get(dep_ref)
            if dep is None or dep.module == entry.module:
                continue  # same-module bases are co-generated (spec §2)
            relpath = _generated_relpath(dep.module, generated_dir=generated_dir)
            gen_path = package_dir / relpath
            try:
                gen_src = gen_path.read_text(encoding="utf-8")
                summary = build_generated_class_api_summary(
                    gen_src, dep.qualname, spec_docstring=""
                )
            except Exception:
                entry_inherited.append(f"unbuilt:{dep_ref!s}")
                continue
            for m in summary.members:
                entry_inherited.append(f"{dep.qualname}.{m.name}{m.signature}")
                if m.doc:
                    entry_inherited.append(f"  doc: {m.doc.splitlines()[0]}")
        inherited_lines.extend(entry_inherited)
        contract_blocks.append(
            render_whole_class_contract(
                class_segment=extract_source_segment(entry),
                base_contract_block=resolve_base_contract(entry.obj).block,  # type: ignore[arg-type]
                inherited_api_block="\n".join(entry_inherited),
            )
        )

    inherited_api_block = "\n".join(inherited_lines)
    return WholeClassContext(
        base_contract_block="\n\n".join(base_blocks),
        inherited_api_block=inherited_api_block,
        whole_class_contract_block="\n\n".join(contract_blocks),
        base_api_digest=(
            hashlib.sha256(inherited_api_block.encode("utf-8")).hexdigest()
            if inherited_api_block
            else ""
        ),
    )
```

Check `SpecApiSummary` member field names in `module_api.py` (`m.name`, `m.signature`,
`m.doc`) and adjust to the real dataclass.

2. `_build_context_digest` gains two kwargs, folded conditionally (byte-compat!):

```python
    for block in (whole_class_contract_block, inherited_api_block):
        if block:
            h.update(block.encode("utf-8"))
            h.update(b"\x00")
```

`build_module_context_artifacts` passes them through (defaults `""`).

3. Replace the two independent render paths:
   - `_component_payload` (~1640): call `wcc = _whole_class_context(component_entries, specs=specs, package_dir=package_dir, generated_dir=generated_dir)` and use `wcc.base_contract_block`, `wcc.whole_class_contract_block` (drop the inline `render_whole_class_contract` loop; keep the scaffold/seed logic as-is), passing the new kwargs into `build_module_context_artifacts`.
   - `_build_module` (~1805): same — replace `base_contract_block=_base_contract_block(entries)` with the helper's outputs and new kwargs.
   - grep `_base_contract_block(` and `build_module_context_artifacts(` across `src/` (notably `status_core.py`, `cli.py`) and thread the helper identically so **status and build compute the same digest from the same on-disk artifacts**. `_base_contract_block` becomes unused → delete it and its imports.

4. Header (~1933): after `"module_api_digest"`, add conditionally:

```python
        if wcc.base_api_digest:
            header_fields["base_api_digest"] = wcc.base_api_digest
```

and in the freshness/refreeze comparison layer add an extractor mirroring
`extract_module_context_digest` (grep `extract_module_context_digest` in
`src/jaunt/header.py` and copy the pattern for `base_api_digest`). A module **with**
cross-module spec'd bases whose stored `base_api_digest` is absent or differs from the
fresh value is stale; modules without such bases ignore the field entirely.

- [ ] **Step 4: Run suite, lint, typecheck.** Every pre-existing context-digest test must pass unmodified.
- [ ] **Step 5: Commit** — `feat(builder): artifact-derived inherited-API context + base_api_digest staleness`

---

### Task 10: Refreeze guard

**Files:**
- Modify: `src/jaunt/builder.py` (`plan_refreeze_or_rebuild`, ~line 442) and its call sites (grep `plan_refreeze_or_rebuild(` — cli/daemon paths)
- Test: wherever refreeze is tested (grep `refrozen` under `tests/` — e.g. `tests/test_semantic_gate*.py` / `tests/test_refreeze*.py`)

**Interfaces:**
- `plan_refreeze_or_rebuild(..., base_api_changed: set[str] | frozenset[str] = frozenset())` — module names whose fresh `base_api_digest` differs from the stored header field (or whose header lacks it while cross-module spec'd bases exist). Callers compute this set from `_whole_class_context` + header extraction (Task 9).

- [ ] **Step 1: Write the failing test**

```python
def test_refreeze_refused_when_base_api_moved(...) -> None:
    plan = await plan_refreeze_or_rebuild(
        ..., stale_modules={"pkg.child"}, base_api_changed={"pkg.child"},
    )
    assert "pkg.child" in plan.rebuild
    assert "pkg.child" not in plan.refrozen
```

(Adapt to the existing refreeze-test fixtures in whichever file covers
`plan_refreeze_or_rebuild` today.)

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** — at the top of the per-module loop in `plan_refreeze_or_rebuild`:

```python
        if module_name in base_api_changed:
            # A spec'd base's generated public API moved (or was never captured):
            # the generated body may genuinely need to change — never refreeze.
            rebuild.add(module_name)
            continue
```

Then update the call sites: compute `base_api_changed` where header fields are already
read (`header_fields_by_module` construction), comparing stored vs fresh
`base_api_digest` per Task 9's extractor.

- [ ] **Step 4: Run suite, lint, typecheck.**
- [ ] **Step 5: Commit** — `feat(builder): refuse refreeze when a base's generated API moved`

---

### Task 11: End-to-end inheritance tests

**Files:**
- Create: `tests/test_inheritance_e2e.py`
- Consumes: everything above; mocked backend fixtures from `tests/test_builder_whole_class.py` (copy its project-scaffolding helpers).

- [ ] **Step 1: Write the tests** (these drive out integration bugs; expect iteration)

Three tests, all against the mocked generator backend:

```python
def test_cross_module_base_builds_first_and_child_sees_inherited_api(tmp_path):
    """pkg/base.py has @magic class A; pkg/child.py has @magic class B(A).
    infer_deps disabled in config. Run the build with a recording fake backend.

    Assert: (1) A's module generated before B's (record call order);
    (2) B's ModuleSpecContext.whole_class_contract_block contains an
    'Inherited generated API' section mentioning a method of generated A;
    (3) both headers written; B's header contains base_api_digest."""


def test_same_module_base_cogenerated_without_conflict(tmp_path):
    """One module defines @magic class A and @magic class B(A) (plus an inner
    @jaunt.magic sealed method on B). Build succeeds as a single component:
    no 'Conflicting @magic' error, one generated module containing both classes."""


def test_child_restaled_by_base_api_change_not_body_change(tmp_path):
    """Build once. Then (a) rewrite A's generated artifact with an added public
    method and recompute staleness (jaunt status path / is_module_stale):
    B must be stale. (b) restore, rewrite only a method body (same signatures
    and docstrings): B must be fresh."""
```

Write real bodies following the fixture idioms in `tests/test_builder_whole_class.py`
(project tmp dir, `jaunt.toml`, fake backend recording `ModuleSpecContext`s). Also add a
runtime `super()` composition test to `tests/test_magic_decorator.py`: hand-write
generated artifacts for `pkg.base`/`pkg.child` where generated `B.method` calls
`super().method()`, import the spec modules through the decorators, and assert
`B().method()` returns the composed result.

- [ ] **Step 2: Run, fix integration fallout, keep the full suite green.**
- [ ] **Step 3: Commit** — `test: end-to-end inheritance coverage (ordering, co-generation, staleness, super())`

---

### Task 12: Documentation

**Files:**
- Modify: `CLAUDE.md` (whole-class bullet in Key Concepts), the `jaunt instructions` primer template (grep `instructions` under `src/jaunt/` for the text source), `docs-site/content/docs/writing-specs/magic.mdx`, `docs-site/content/docs/writing-specs/dependencies.mdx`, `docs-site/content/docs/reference/change-detection.mdx`, and the bundled jaunt skill (grep `whole-class` under `.claude/skills/`).

- [ ] **Step 1: CLAUDE.md** — extend the whole-class bullet with the tier table in prose: preserved (`@jaunt.preserve`, hand-written, verbatim) / sealed (inner `@jaunt.magic` on a stub, signature enforced exactly) / guidepost (unmarked stub, model may adapt); note that a spec'd base class in the header is an always-on dependency edge (not gated by `infer_deps`) and that a cross-module base's generated public API participates in staleness.
- [ ] **Step 2: instructions primer** — add the tier vocabulary + one sealed example (a 4-line class).
- [ ] **Step 3: docs-site** — `magic.mdx`: tiers table + sealed example; `dependencies.mdx`: base edges are structural; `change-detection.mdx`: `base_api_digest` propagation (signature *or docstring* change in a base's generated public API restales subclasses; body-only changes don't).
- [ ] **Step 4: skill** — mirror the CLAUDE.md bullet if the skill documents whole-class magic.
- [ ] **Step 5: Run suite (docs shouldn't break it), commit** — `docs: three-tier class interface vocabulary + inheritance-aware builds`

---

## Execution notes

- Tasks 1→7 are strictly ordered by interface dependency. Task 8 depends on Task 2; Task 9 on Tasks 3, 5, 8; Task 10 on Task 9; Task 11 on all; Task 12 anytime after 9.
- The riskiest tasks are 3 (absorption — import-order semantics) and 9 (digest wiring across build/status call sites). If Task 9's call-site sweep finds a context-digest computation this plan didn't anticipate, thread the same helper through it — the invariant is: **one helper, identical inputs, everywhere a module context digest is computed.**
- Byte-compat check before finishing: `git stash` nothing — instead run the full pre-existing digest/header test files untouched; if any needed edits beyond new tests, that's a compat violation to fix, not a test to update (except the explicitly reworked guard test in Task 4 and old contract-render wording in Task 8).
