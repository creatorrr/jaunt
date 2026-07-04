# `jaunt.magic_module` (1.4.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is executed by a dynamic Workflow of opus@high subagents driving `codex exec` (model `gpt-5.5`, reasoning_effort=high) in a dedicated worktree; each task below is one workflow unit with its own test cycle and commit(s).

**Goal:** Ship `jaunt.magic_module(__name__)` — module-level magic activation where every top-level stub becomes a spec without per-symbol decorators — as the new primary authoring style, with full `@magic` kwarg parity, Approach-A runtime forwarding, digest parity with decorator mode, and a thorough docs-site + README repositioning.

**Architecture:** A new focused module `src/jaunt/module_magic.py` owns the AST scan/classification, the `magic_module()` entry point, the `_MagicModule` runtime hook, and the post-import finalize step. Registration is two-phase: AST-register at call time with `obj=None`, then `import_and_collect` backfills `obj` and runs the SAME analysis pipeline decorator entries get (`analyze_magic_decorators`, absorption, `resolve_base_contract`) so module-origin entries are shape-identical to decorator entries — no `None` ever reaches builder/tester/CLI, and digests match byte-for-byte for unchanged stub bodies.

**Tech Stack:** Python 3.12+, uv, pytest (mocked generator backend — no API keys), ruff (line-length 100, E/F/I/UP/B), ty.

**Spec:** `docs/superpowers/specs/2026-07-04-magic-module-design.md` (commit `0258687`) — read it before starting any task. Section references (§1–§8) below point into it.

## Global Constraints

- The spec is the contract. Where this plan deviates it says so explicitly (two deviations: direct `ast.parse` instead of the build-side parse cache in Task 3; `"origin": "module" | "decorator"` JSON values in Task 7).
- Strict stub forms only (`class_analysis.is_stub_body`): docstring-only, `...`, `pass`, `raise NotImplementedError`. `raise RuntimeError("spec stub")` is NOT a stub in module mode.
- Non-jaunt-decorated top-level defs/classes are NEVER governed by the module scan. Jaunt-decorator skip detection must be alias-aware.
- Module defaults merge into `SpecEntry.decorator_kwargs` key-by-key; per-symbol decorator kwargs win per key; the merge NEVER applies to method-level entries (`class_name is not None`) — inner `@sig`/`@magic` method specs must stay kwarg-free or `_absorb_method_specs` raises.
- All existing tests stay green in every task's commit; run `uv run pytest` (full), `uv run ruff check .`, `uv run ty check` before each commit.
- Do not touch `__generated__/` content in `examples/` by hand.
- Commit messages: conventional commits (`feat:`, `fix:`, `docs:`, `test:`).
- Docs prose (Tasks 8–9): writer agents MUST load `/home/diwank/github.com/creatorrr/jaunt/.claude/skills/natural-writing/` before writing.

## Wave map

| Wave | Tasks | Parallel? |
|------|-------|-----------|
| 1 | 1 (scan + classification), 2 (registry extensions) | yes — disjoint files |
| 2 | 3 (magic_module call + runtime hook), 4 (decorator-side merge) | yes — module_magic.py vs runtime.py |
| 3 | 5 (finalize + prescreen), then 6 (digest parity battery) | 6 after 5 |
| 4 | 7 (tooling: specs/init/e2e), 8 (README + in-repo docs), 9 (docs-site sweep) | yes — disjoint files |
| 5 | 10 (integration gate + version bump) | last |

File-overlap warnings: Tasks 1, 3, 5 all touch `module_magic.py` — they are in different waves, later tasks extend the file. Task 7 touches `cli.py` and `init_template.py` only. Tasks 8 and 9 are prose-only and disjoint (repo root vs `docs-site/`).

---

### Task 1: Module scan & classification (`module_magic.py` part 1)

**Files:**
- Create: `src/jaunt/module_magic.py`
- Test: `tests/test_module_magic_scan.py` (new)

**Interfaces:**
- Produces:
  - `ModuleSpecCandidate` (frozen dataclass): `name: str`, `is_class: bool`.
  - `ModuleScan` (frozen dataclass): `candidates: tuple[ModuleSpecCandidate, ...]`, `warnings: tuple[str, ...]`.
  - `scan_module_source(tree: ast.Module, *, module: str) -> ModuleScan` — pure function, no registry access, no I/O.
  - `_jaunt_decorator_aliases(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]` — `(module_aliases, member_aliases)` resolved from the module's own import statements.
- Consumes: `class_analysis.is_stub_body`.

Classification rules (spec §1 table, implement exactly):

1. Resolve aliases first. `import jaunt` → module alias `jaunt`; `import jaunt as j` → `j`. `from jaunt import magic` → member alias `magic`; `from jaunt import magic as m` → `m`; accept source modules `jaunt` and `jaunt.runtime`. Member set: `{"magic", "sig", "preserve", "test", "contract"}` (aliased per import). A decorator expression matches when it is `Attribute(value=Name(id ∈ module_aliases), attr ∈ {"magic","sig","preserve","test","contract"})` or `Name(id ∈ member_aliases)`, in bare or called form.
2. For each DIRECT child of `tree.body` that is `FunctionDef` / `AsyncFunctionDef`:
   - any jaunt decorator → **skip** (the decorator governs it — this includes `@jaunt.preserve`, the documented opt-out for intentionally-empty handwritten functions);
   - else any decorator at all → **handwritten** (never governed — closes the `@typing.overload` / `@property` / `@functools.cache` traps);
   - else `is_stub_body(node)` → **spec** (function candidate);
   - else → **handwritten**.
3. For each direct `ClassDef` child:
   - any jaunt decorator → **skip**; else any non-jaunt decorator (`@dataclass`, …) → **handwritten**;
   - else **spec** (class candidate) when the body is docstring-only (every stmt is `Expr(Constant)` or `Pass`) OR at least one direct method (`FunctionDef`/`AsyncFunctionDef` in `class.body`) has `is_stub_body(m)` true and does not carry a `@jaunt.preserve`-matching decorator (alias-aware);
   - else → **handwritten**.
4. Anything else in `tree.body` (assignments, `if` blocks, `try` blocks, imports) is never scanned; defs under conditional branches are neither governed nor guaranteed as context (§1, documented — no code needed).
5. Capture warnings (spec §3 mitigation part 1), computed AFTER the candidate set is known. Let `spec_names = {c.name for c in candidates}`. Walk `tree.body` direct children:
   - any `ClassDef` (candidate or not) with a base `Name(id ∈ spec_names)` → warning `"<module>: class '<cls>' subclasses governed spec '<base>' at module level; it will see the pre-rebind stub. Move the subclass into a function or mark '<base>' with an explicit @jaunt.magic."`;
   - any top-level `Expr` / `Assign` / `AnnAssign` whose value subtree (via `ast.walk`) contains `Call(func=Name(id ∈ spec_names))` → warning `"<module>: module-level code calls governed spec '<name>' before rebinding; it will see the pre-rebind stub. Move the call into a function."`
   Warnings are deterministic: sorted by (lineno, message).

- [ ] **Step 1: Write the failing tests.** `tests/test_module_magic_scan.py`, one test per classification-table row, driven by inline `textwrap.dedent` sources parsed with `ast.parse`:

```python
import ast
import textwrap

from jaunt.module_magic import ModuleSpecCandidate, scan_module_source


def _scan(src: str):
    return scan_module_source(ast.parse(textwrap.dedent(src)), module="m")


def test_ellipsis_and_docstring_and_pass_and_nie_bodies_are_specs():
    scan = _scan("""
        def a(x: int) -> int:
            ...
        def b():
            "doc only"
        def c():
            pass
        async def d():
            raise NotImplementedError
    """)
    assert {c.name for c in scan.candidates} == {"a", "b", "c", "d"}
    assert all(not c.is_class for c in scan.candidates)


def test_runtime_error_body_is_not_a_stub():
    scan = _scan("""
        def f():
            raise RuntimeError("spec stub")
    """)
    assert scan.candidates == ()


def test_real_body_is_handwritten():
    scan = _scan("""
        def f(x):
            return x + 1
    """)
    assert scan.candidates == ()


def test_docstring_only_class_is_whole_class_spec():
    scan = _scan("""
        class Email:
            \"\"\"Email object.\"\"\"
    """)
    assert scan.candidates == (ModuleSpecCandidate(name="Email", is_class=True),)


def test_class_with_one_stub_method_is_spec_and_all_real_is_not():
    scan = _scan("""
        class Mixed:
            def done(self):
                return 1
            def todo(self):
                \"\"\"stub\"\"\"
        class Done:
            def done(self):
                return 1
    """)
    assert {c.name for c in scan.candidates} == {"Mixed"}


def test_jaunt_decorated_defs_are_skipped_plain_and_aliased():
    scan = _scan("""
        import jaunt
        import jaunt as j
        from jaunt import magic as m

        @jaunt.magic()
        def a(): ...
        @j.magic
        def b(): ...
        @m
        def c(): ...
        @jaunt.preserve
        def intentionally_empty(): ...
    """)
    assert scan.candidates == ()


def test_non_jaunt_decorated_defs_are_never_governed():
    scan = _scan("""
        import typing
        import functools
        from dataclasses import dataclass

        @typing.overload
        def f(x: int) -> int: ...
        @typing.overload
        def f(x: str) -> str: ...
        def f(x): return x

        @property
        def broken(self): ...
        @functools.cache
        def cached(): ...

        @dataclass
        class Config:
            \"\"\"fields\"\"\"
    """)
    assert scan.candidates == ()


def test_conditional_defs_are_not_governed():
    scan = _scan("""
        if True:
            def f(): ...
    """)
    assert scan.candidates == ()


def test_capture_warnings_for_toplevel_subclass_and_call():
    scan = _scan("""
        class Email:
            \"\"\"spec\"\"\"
        def parse(raw: str) -> Email:
            ...
        class Signed(Email):
            def sign(self): return 1
        DEFAULT = parse("x")
    """)
    assert len(scan.warnings) == 2
    assert "Signed" in scan.warnings[0] and "Email" in scan.warnings[0]
    assert "parse" in scan.warnings[1]
```

- [ ] **Step 2: Run to verify failure.** `uv run pytest tests/test_module_magic_scan.py -v` → FAIL with `ModuleNotFoundError: jaunt.module_magic`.
- [ ] **Step 3: Implement** `src/jaunt/module_magic.py` with the two dataclasses, `_jaunt_decorator_aliases`, a private `_matches_jaunt_decorator(dec, module_aliases, member_aliases, members=frozenset(...))` helper (reuse the `dec.func if isinstance(dec, ast.Call) else dec` unwrap idiom from `class_analysis.py`), and `scan_module_source` per the rules above. Module docstring in the repo's style (one evocative line + explanation). No registry imports in this task.
- [ ] **Step 4: Run tests to verify pass.** `uv run pytest tests/test_module_magic_scan.py -v` → PASS.
- [ ] **Step 5: Full suite, lint, typecheck, commit.** `uv run pytest && uv run ruff check . && uv run ty check`, then `git commit -m "feat: module-magic AST scan and classification"`.

---

### Task 2: Registry extensions — defaults registry, origin, duplicate-origin warning

**Files:**
- Modify: `src/jaunt/registry.py`
- Test: `tests/test_registry.py` (extend; create if absent)

**Interfaces:**
- Produces:
  - `SpecEntry.origin: Literal["decorator", "module"] = "decorator"` — new field, defaulted so every existing construction site stays valid.
  - `ModuleMagicDefaults` (frozen dataclass): `module: str`, `source_file: str`, `decorator_kwargs: dict[str, object]`.
  - `register_module_magic(defaults: ModuleMagicDefaults) -> None`, `get_module_magic_defaults(module: str) -> ModuleMagicDefaults | None`, `get_module_magic_registry() -> dict[str, ModuleMagicDefaults]`.
  - `clear_registries()` also clears the module-magic registry.
  - `register_magic` warns (`warnings.warn(..., UserWarning, stacklevel=2)`) when overwriting an entry whose `origin` differs: `f"jaunt spec {entry.spec_ref!s} registered from both a module scan and a decorator; the decorator registration wins. This usually means the module scan failed to skip a decorated symbol (aliased import?)."` Last-write-wins stays the behavior (spec §2 defense-in-depth).
- Consumed by: Tasks 3, 4, 5, 7.

- [ ] **Step 1: Write the failing tests** (in `tests/test_registry.py`, following existing registry test style — check whether the file exists first and extend it):

```python
import warnings

import pytest

from jaunt.registry import (
    ModuleMagicDefaults,
    SpecEntry,
    clear_registries,
    get_module_magic_defaults,
    register_magic,
    register_module_magic,
)
from jaunt.spec_ref import normalize_spec_ref


def _entry(origin: str) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref("m:f"),
        module="m",
        qualname="f",
        source_file="m.py",
        obj=None,
        decorator_kwargs={},
        origin=origin,
    )


def test_module_magic_defaults_roundtrip_and_clear():
    clear_registries()
    register_module_magic(
        ModuleMagicDefaults(module="m", source_file="m.py", decorator_kwargs={"prompt": "p"})
    )
    assert get_module_magic_defaults("m").decorator_kwargs == {"prompt": "p"}
    assert get_module_magic_defaults("other") is None
    clear_registries()
    assert get_module_magic_defaults("m") is None


def test_register_magic_warns_on_cross_origin_overwrite():
    clear_registries()
    register_magic(_entry("module"))
    with pytest.warns(UserWarning, match="module scan and a decorator"):
        register_magic(_entry("decorator"))


def test_register_magic_same_origin_overwrite_is_silent():
    clear_registries()
    register_magic(_entry("module"))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        register_magic(_entry("module"))
```

- [ ] **Step 2: Run to verify failure.** `uv run pytest tests/test_registry.py -v` → FAIL (unknown field / import errors).
- [ ] **Step 3: Implement.** Add the field (mind the frozen/slots dataclass — new field goes after `decorator_warnings` with its default), the dataclass, `_MODULE_MAGIC_REGISTRY: dict[str, ModuleMagicDefaults]`, the three functions, the `clear_registries` line, and the overwrite warning in `register_magic`. `obj: object` already admits `None`; loosen the annotation to `obj: object | None` so ty is explicit about phase one.
- [ ] **Step 4: Run tests to verify pass**, then the full suite (the `SpecEntry` field addition must not break any existing constructor call — all pass it positionally up to `decorator_kwargs` and by keyword after).
- [ ] **Step 5: Lint, typecheck, commit.** `git commit -m "feat: module-magic defaults registry, SpecEntry.origin, cross-origin overwrite warning"`.

---

### Task 3: `magic_module()` — call-time registration + Approach-A runtime hook

**Files:**
- Modify: `src/jaunt/module_magic.py` (extend Task 1's file)
- Modify: `src/jaunt/__init__.py` (export)
- Test: `tests/test_magic_module_runtime.py` (new), with fixture spec modules under `tests/` per the existing pattern (look at how `tests/test_runtime.py` / builder tests create importable temp modules — reuse that pattern: `tmp_path` + `monkeypatch.syspath_prepend` + `JAUNT_GENERATED_DIR` untouched).

**Interfaces:**
- Consumes: Task 1's `scan_module_source`; Task 2's `ModuleMagicDefaults`, `register_module_magic`, `get_module_magic_defaults`, `register_magic`; existing `runtime._not_built_error`, `paths.spec_module_to_generated_module`, `spec_ref.normalize_spec_ref`, `errors.JauntError`.
- Produces:
  - `magic_module(name: str, *, deps: object | None = None, prompt: object | None = None, infer_deps: object | None = None, test: object | None = None) -> None` — exported as `jaunt.magic_module`, added to `__all__`.
  - `_MagicModule(types.ModuleType)` and module-dict state keys `"__jaunt_magic_module__"` (a small `_ModuleMagicState` frozen dataclass: `module: str`, `spec_names: frozenset[str]`, `class_names: frozenset[str]`) and `"__jaunt_original_stubs__"` (snapshot dict, written by resolution; read by Task 5's finalize).

Behavior, in order (spec §2 phase one, §3, §5):

1. **Call-site validation:** caller frame's `f_code.co_name` must be `"<module>"` (one `f_back` hop from inside `magic_module`) → else `JauntError("jaunt.magic_module(...) must be called at module top level, before the definitions it governs.")`.
2. `name not in sys.modules` → `JauntError(f"magic_module({name!r}): no such module in sys.modules; pass __name__ from the module being governed.")`.
3. Already governed (`get_module_magic_defaults(name) is not None`) → `JauntError(f"magic_module() was already called for module {name!r}; one governing call per module.")`.
4. **Kwargs:** build `decorator_kwargs` exactly like `runtime.magic` does (include a key only when the kwarg is not `None`; `test` must be `bool` or `JauntError("magic_module(test=...) must be a boolean when provided.")`).
5. **Parse:** `source_file = sys.modules[name].__file__`; `ast.parse(Path(source_file).read_text(encoding="utf-8"))`. *Deviation from spec §2 ("via the existing parse cache"), deliberate:* `ParseCache` needs a config-resolved cache dir which does not exist at import time in user processes; `decorator_analysis.analyze_magic_decorators` parses directly for the same reason. Note the deviation in the commit body.
6. **Scan:** `scan = scan_module_source(tree, module=name)`. Zero candidates → `warnings.warn(f"magic_module({name!r}): no top-level stubs classified as specs (all bodies are real or decorated). Legal during gradual conversion; check placement if unexpected.", UserWarning, stacklevel=2)` and still register defaults + install nothing further (no hook needed with zero names — but still register the defaults so decorated symbols in the module inherit them).
7. **Register defaults:** `register_module_magic(ModuleMagicDefaults(module=name, source_file=source_file, decorator_kwargs=decorator_kwargs))`.
8. **Register entries** for each candidate: `SpecEntry(kind="magic", spec_ref=normalize_spec_ref(f"{name}:{c.name}"), module=name, qualname=c.name, source_file=source_file, obj=None, decorator_kwargs=dict(decorator_kwargs), class_name=None, origin="module")`.
9. **Emit scan warnings:** `warnings.warn(w, UserWarning, stacklevel=2)` for each.
10. **Install the hook** (only when candidates exist): write `_ModuleMagicState` into the module `__dict__`, then `mod.__class__ = _MagicModule` (precedent: `contract/__init__.py:38-41`).

`_MagicModule.__getattribute__(self, attr)`:

- Fetch `d = object.__getattribute__(self, "__dict__")`, `state = d.get("__jaunt_magic_module__")`.
- If `state is None` or `attr not in state.spec_names` → fall through to `ModuleType.__getattribute__(self, attr)`.
- If any spec name is missing from `d` (module still executing / circular import) → fall through WITHOUT resolving (spec §3: circulars bypass resolution).
- Else `_resolve_module(self, state, d)` then fall through.

`_resolve_module(mod, state, d)` (never triggers `__getattribute__` recursively — use only `d` and `object.__setattr__`-free dict writes):

1. Snapshot: `d.setdefault("__jaunt_original_stubs__", {n: d[n] for n in state.spec_names})`.
2. Try `importlib.import_module(spec_module_to_generated_module(state.module, generated_dir=os.environ.get("JAUNT_GENERATED_DIR", "__generated__")))` — reuse `runtime._import_generated_module` if importable without cycles (it is: `module_magic` may import from `jaunt.runtime` freely).
3. **Built:** for each spec name, `gen = getattr(gen_mod, name, _MISSING)`. Present: if a class, stamp `gen.__jaunt_spec_ref__ = f"{module}:{name}"` and `gen.__module__ = state.module` (parity with the decorator built path, runtime.py:366-368); rebind `d[name] = gen`. Absent from the generated module: fall through to the not-built shape for that one name.
4. **Not built** (`ModuleNotFoundError`): for each function name, bind a raiser `def _raiser(*a, **k): raise _not_built_error(ref)` carrying `__name__`/`__qualname__`/`__module__` of the spec; for each class name, bind the `__new__`-raiser placeholder type exactly as runtime.py:352-359 builds it.
5. Swap back: `mod.__class__ = types.ModuleType`. Steady-state interception cost: zero.

- [ ] **Step 1: Write the failing tests.** `tests/test_magic_module_runtime.py`. Test moves: write a governed spec module and a fake generated counterpart to `tmp_path`, `monkeypatch.syspath_prepend(tmp_path)`, import, assert. Cover:

```python
# Governed module fixture (written by a helper into tmp_path / "gm_mod.py"):
GOVERNED = '''
import jaunt

jaunt.magic_module(__name__, prompt="module prompt")

class Email:
    """Email object."""

def parse_email(raw: str) -> "Email":
    """Parse."""
    ...

def helper(raw: str) -> str:
    return parse_email(raw).subject
'''

# Generated counterpart at tmp_path / "__generated__" / "gm_mod.py":
GENERATED = '''
class Email:
    def __init__(self, subject: str = "s"):
        self.subject = subject

def parse_email(raw: str) -> Email:
    return Email(subject=raw)
'''
```

  - **registration:** importing the governed module (no generated dir yet) registers `gm_mod:Email` and `gm_mod:parse_email` with `origin == "module"`, `obj is None`, `decorator_kwargs == {"prompt": "module prompt"}`; `get_module_magic_defaults("gm_mod")` is set. (`registry.clear_registries()` + `sys.modules.pop` between tests.)
  - **built path:** with the generated file present, `import gm_mod; gm_mod.parse_email("x").subject == "x"`; `from gm_mod import Email` then `isinstance(gm_mod.parse_email("x"), Email)`; sibling call `gm_mod.helper("t") == "t"`; `Email.__jaunt_spec_ref__ == "gm_mod:Email"`; after first access `type(gm_mod) is types.ModuleType` (swap-back verified).
  - **not-built path:** without the generated file, `gm_mod.parse_email("x")` raises `JauntNotBuiltError` matching `"jaunt build"`; `gm_mod.Email()` raises the same; swap-back still happens.
  - **errors (§5):** calling `jaunt.magic_module("gm_mod")` from inside a function → `JauntError` matching `"module top level"`; a fixture module calling it twice → `JauntError` matching `"already called"`; `jaunt.magic_module("no_such_module_xyz")` from a module body → `JauntError` matching `"sys.modules"`.
  - **zero-spec warning:** a governed module whose only def has a real body → `pytest.warns(UserWarning, match="no top-level stubs")`, and defaults still registered.
  - **capture warning surfaces:** the Task 1 fixture with `class Signed(Email)` imported for real → `pytest.warns(UserWarning, match="pre-rebind stub")`.
  - **`@jaunt.sig` top-level in a governed module** still raises the existing "whole-class" `JauntError` (no new code — regression pin).
- [ ] **Step 2: Run to verify failure.** `uv run pytest tests/test_magic_module_runtime.py -v` → FAIL (`magic_module` not exported).
- [ ] **Step 3: Implement** per the numbered behavior above; export from `jaunt/__init__.py` (`from jaunt.module_magic import magic_module`, add `"magic_module"` to `__all__` right after `"magic"`). Keep `module_magic.py` free of imports from `builder`/`cli` (runtime-importable, like `runtime.py`).
- [ ] **Step 4: Run tests to verify pass.**
- [ ] **Step 5: Full suite, lint, typecheck, commit.** `git commit -m "feat: jaunt.magic_module — module-level magic with intercept-once runtime forwarding"`.

---

### Task 4: Decorator-side kwarg merge in `runtime._decorate`

**Files:**
- Modify: `src/jaunt/runtime.py` (inside `magic()._decorate`, after `decorator_kwargs` construction, runtime.py:283-293)
- Test: `tests/test_magic_module_merge.py` (new)

**Interfaces:**
- Consumes: Task 2's `get_module_magic_defaults`.
- Produces: decorator-registered magic entries in a governed module carry `{**module_defaults.decorator_kwargs, **own_kwargs}` — per-symbol wins per key. Merge applies ONLY when `class_name is None` (top-level function/class specs). Method-level entries (inner `@sig`/`@magic`) are NEVER merged — `_absorb_method_specs` rejects kwargs on them (runtime.py:481-486) and module defaults must not trip that error.

- [ ] **Step 1: Write the failing tests.** Register defaults directly via the registry API (no import machinery needed), then decorate:

```python
import jaunt
from jaunt.registry import (
    ModuleMagicDefaults,
    clear_registries,
    get_magic_registry,
    register_module_magic,
)


def test_decorated_symbol_inherits_module_defaults_per_key(monkeypatch):
    clear_registries()
    register_module_magic(
        ModuleMagicDefaults(
            module=__name__,
            source_file=__file__,
            decorator_kwargs={"prompt": "module prompt", "infer_deps": False},
        )
    )

    @jaunt.magic(deps=["json"])
    def decorated_spec(x: int) -> int:
        ...

    entry = next(
        e for e in get_magic_registry().values() if e.qualname == "decorated_spec"
    )
    assert entry.decorator_kwargs["prompt"] == "module prompt"
    assert entry.decorator_kwargs["infer_deps"] is False
    assert entry.decorator_kwargs["deps"] == ["json"]
    assert entry.origin == "decorator"


def test_per_symbol_kwarg_wins_over_module_default():
    clear_registries()
    register_module_magic(
        ModuleMagicDefaults(
            module=__name__, source_file=__file__, decorator_kwargs={"prompt": "module"}
        )
    )

    @jaunt.magic(prompt="mine")
    def override_spec() -> None:
        ...

    entry = next(e for e in get_magic_registry().values() if e.qualname == "override_spec")
    assert entry.decorator_kwargs["prompt"] == "mine"


def test_no_module_entry_means_no_merge():
    clear_registries()

    @jaunt.magic()
    def plain_spec() -> None:
        ...

    entry = next(e for e in get_magic_registry().values() if e.qualname == "plain_spec")
    assert entry.decorator_kwargs == {}


def test_whole_class_with_sig_method_in_governed_module_does_not_trip_absorption():
    clear_registries()
    register_module_magic(
        ModuleMagicDefaults(
            module=__name__, source_file=__file__, decorator_kwargs={"prompt": "module"}
        )
    )

    @jaunt.magic()
    class Sealed:
        """Whole-class spec."""

        @jaunt.sig
        def method(self, x: int) -> int:
            ...

    class_entry = next(e for e in get_magic_registry().values() if e.qualname == "Sealed")
    assert class_entry.sealed_members == ("method",)
    assert class_entry.decorator_kwargs["prompt"] == "module"
```

  (Note: these decorate inside a test function — `_resolve_magic_identity` falls back to the callsite resolver for local defs; follow the existing pattern used by current runtime tests. If existing runtime tests define specs at module scope in fixture files instead, mirror that pattern rather than fighting the identity resolver.)
- [ ] **Step 2: Run to verify failure** (`prompt` missing from merged kwargs; absorption test raising `JauntError` if the guard is missing).
- [ ] **Step 3: Implement.** In `_decorate`, after the kwargs block and BEFORE `_absorb_method_specs`/entry construction:

```python
if class_name is None:
    module_defaults = get_module_magic_defaults(module)
    if module_defaults is not None:
        decorator_kwargs = {**module_defaults.decorator_kwargs, **decorator_kwargs}
```

  Import `get_module_magic_defaults` in the existing `from jaunt.registry import (...)` block. Do NOT touch `test()`/`contract()` — the merge is a magic-spec concept.
- [ ] **Step 4: Run tests to verify pass; full suite.**
- [ ] **Step 5: Lint, typecheck, commit.** `git commit -m "feat: decorated specs inherit magic_module defaults key-by-key"`.

---

### Task 5: Post-import finalize (obj backfill + analysis parity) and discovery prescreen

**Files:**
- Modify: `src/jaunt/module_magic.py` (add `finalize_module_magic`)
- Modify: `src/jaunt/discovery.py` (`import_and_collect` finalize sweep, discovery.py:326-337; `_has_jaunt_markers`, discovery.py:188-216)
- Test: `tests/test_magic_module_finalize.py` (new), extend `tests/test_discovery.py` (prescreen cases)

**Interfaces:**
- Produces: `finalize_module_magic(module_name: str) -> None` — no-op for ungoverned/unimported modules; after it runs, every module-origin entry of that module has: real `obj` (pre-rebind stub via the `__jaunt_original_stubs__` snapshot when present, else the live module attribute), `auto_deps`/`decorator_api_records`/`effective_signature`/`effective_signature_source`/`decorator_warnings` from `analyze_magic_decorators(module=..., qualname=..., source_file=..., decorated_obj=obj)`, and for class entries `sealed_members` from `_absorb_method_specs(obj, module=module_name, class_name=entry.qualname)` plus `base_deps` from `resolve_base_contract(obj).project_base_refs` (normalize + sort, exactly the decorator path at runtime.py:307-316).
- Consumes: Task 3's snapshot key and registry state; `dataclasses.replace` for the frozen `SpecEntry`.
- `import_and_collect(module_names, *, kind)` gains a final sweep: after the import loop, when `kind == "magic"`, iterate `get_module_magic_registry()` and call `finalize_module_magic(m)` for every governed module found in `sys.modules` (covers modules imported transitively, not just those in `module_names`). Import `finalize_module_magic` lazily inside the function to keep discovery light.
- `_has_jaunt_markers` additionally returns True for any `ast.Expr` whose value is a `Call` with func `Name(id == "magic_module")` or `Attribute(attr == "magic_module")` (belt-and-braces; the `import jaunt` branch already passes for the normal case). Add `"magic_module"` handling inside the existing `ast.walk` loop.

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_magic_module_finalize.py — reuse Task 3's governed-module fixture helper.

def test_finalize_backfills_obj_and_analysis(tmp_path, monkeypatch):
    _write_governed_module(tmp_path)  # gm_mod.py from Task 3, no generated dir
    monkeypatch.syspath_prepend(str(tmp_path))
    registry.clear_registries()
    discovery.import_and_collect(["gm_mod"], kind="magic")

    entries = {e.qualname: e for e in registry.get_magic_registry().values()}
    fn = entries["parse_email"]
    assert callable(fn.obj) and fn.obj is not None
    assert fn.effective_signature is not None          # same rendering path as decorators
    cls = entries["Email"]
    assert isinstance(cls.obj, type)                    # passes builder's isinstance gates
    assert cls.origin == "module"


def test_finalize_uses_prerebind_snapshot_when_access_already_fired(tmp_path, monkeypatch):
    _write_governed_module(tmp_path)
    _write_generated_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    registry.clear_registries()
    mod = importlib.import_module("gm_mod")
    _ = mod.parse_email("x")                            # fires resolution, rebinds
    finalize_module_magic("gm_mod")
    entry = next(e for e in registry.get_magic_registry().values() if e.qualname == "parse_email")
    assert entry.obj is mod.__dict__["__jaunt_original_stubs__"]["parse_email"]


def test_finalize_absorbs_sig_methods_and_base_deps(tmp_path, monkeypatch):
    # governed module: class with a @jaunt.sig method; a second governed class
    # subclassing a spec'd base DECORATED with @jaunt.magic in another module.
    ...
    assert cls_entry.sealed_members == ("method",)
    # base_deps parity with decorator mode (runtime.py:307-316)


def test_transitively_imported_governed_module_is_finalized(tmp_path, monkeypatch):
    # importer.py: `import gm_mod`; import_and_collect(["importer"], kind="magic")
    # → gm_mod entries still have obj backfilled by the final sweep.
    ...

# tests/test_discovery.py additions:

def test_prescreen_recognizes_magic_module_call():
    src = "import jaunt\njaunt.magic_module(__name__)\n"
    assert _has_jaunt_markers(src)

def test_prescreen_recognizes_bare_magic_module_call_without_import():
    # belt-and-braces branch: call form alone, no importable jaunt alias
    src = "from spam import magic_module\nmagic_module(__name__)\n"
    assert _has_jaunt_markers(src)
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** `finalize_module_magic` + the `import_and_collect` sweep + the prescreen branch. Ordering inside finalize per entry: absorb methods FIRST (so `analyze_magic_decorators` and the registered entry see the restored original functions), then base_deps, then analysis, then `register_magic(dataclasses.replace(entry, ...))` (same-origin overwrite — silent by Task 2's rule).
- [ ] **Step 4: Run tests to verify pass; full suite** (builder/tester/CLI suites now exercise entries that always have real `obj` — spec §2's P1 requirement).
- [ ] **Step 5: Lint, typecheck, commit.** `git commit -m "feat: finalize module-magic entries post-import — obj backfill with full decorator-pipeline parity"`.

---

### Task 6: Digest parity & neutrality battery

**Files:**
- Test: `tests/test_magic_module_digest.py` (new)
- Modify (only if a test exposes drift): `src/jaunt/digest.py`, `src/jaunt/module_magic.py`

**Interfaces:**
- Consumes: `digest.local_digest`, `digest.structural_digest`, `digest.prose_digest`; the import path from Tasks 3+5 (entries must be POST-finalize when digested — that is what the builder sees).

This task is the spec's two P1 verification battery (§2 "digest neutrality, stated precisely"). Expected to be tests-only; any failure is a bug in Tasks 3–5 and gets fixed here.

- [ ] **Step 1: Write the tests.** Each scenario builds two sibling fixture modules in `tmp_path` (one decorator-style, one module-style), imports + finalizes both via `discovery.import_and_collect`, then compares digests of the corresponding entries:

```python
def test_conversion_with_unchanged_ellipsis_body_is_digest_neutral():
    # dec_mod.py:  @jaunt.magic()\ndef f(x: int) -> str:\n    """D."""\n    ...
    # mod_mod.py:  jaunt.magic_module(__name__)\ndef f(x: int) -> str:\n    """D."""\n    ...
    assert local_digest(dec_entry) == local_digest(mod_entry)          # includes prose
    assert structural_digest(dec_entry) == structural_digest(mod_entry)
    assert dec_entry.effective_signature == mod_entry.effective_signature  # P1: same rendering path


def test_conversion_neutral_for_docstring_only_class():
    ...  # class Email: """doc""" — decorator vs module style, digests equal


def test_runtime_error_body_conversion_restales():
    # dec: @jaunt.magic() def f(): """D."""\n    raise RuntimeError("spec stub")
    # mod: def f(): """D."""\n    ...
    assert structural_digest(dec_entry) != structural_digest(mod_entry)  # documented restale


def test_module_prompt_edit_restales_every_governed_spec():
    # same module-style fixture registered twice (clear registries between),
    # prompt="v1" vs prompt="v2" → structural digests differ for BOTH specs
    ...


def test_per_symbol_override_wins_in_digest():
    # governed module with @jaunt.magic(prompt="mine") on one symbol:
    # its digest reflects "mine"; the sibling's reflects the module default
    ...


def test_module_kwargs_reach_decorated_symbols_digest():
    # decorated symbol in a governed module digests differently than the same
    # decorated symbol in an ungoverned module (merge feeds _stable_decorator_kwargs)
    ...
```

- [ ] **Step 2: Run.** `uv run pytest tests/test_magic_module_digest.py -v`. Diagnose any failure to its source task (signature rendering → Task 5's analysis call; kwargs → Task 3/4 merge) and fix in this task's commit.
- [ ] **Step 3: Full suite, lint, typecheck, commit.** `git commit -m "test: digest parity battery — decorator↔module conversion neutrality and restale rules"`.

---

### Task 7: Tooling — `jaunt specs` origin, init template, mocked-backend e2e

**Files:**
- Modify: `src/jaunt/cli.py` (`cmd_specs`, cli.py:3855-3918)
- Modify: `src/jaunt/init_template.py` (starter spec, near line 174)
- Test: extend `tests/test_cli.py` (specs command cases), `tests/test_init.py` or nearest init test module, new `tests/test_magic_module_build.py`

**Interfaces:**
- `cmd_specs` spec_list items gain `"origin": entry.origin` and `"kwargs": entry.decorator_kwargs` (JSON mode); text mode appends ` [module]` for module-origin entries and the merged kwargs when non-empty: `- gm_mod:parse_email (gm_mod.py) [module] kwargs={'prompt': 'module prompt'}`. *Deviation from spec §4 (which sketches `"origin": "magic_module"`):* emit the `SpecEntry.origin` values `"module"` / `"decorator"` — symmetric and already tested; note in commit body.
- `init_template.py` starter module switches to magic_module style:

```python
import jaunt

jaunt.magic_module(__name__)


def greet(name: str) -> str:
    """Return a friendly greeting for `name`.

    Includes the name verbatim and ends with an exclamation mark.
    """
    ...
```

  (Replace the current `@jaunt.magic()` starter; keep any surrounding template text consistent. `jaunt init` output must import-run clean.)
- e2e (mocked generator backend, no API key — follow the existing mocked-build test pattern in `tests/test_builder_io.py` / `tests/test_cli.py`): a governed module builds end-to-end — generated file written to the right layout, validation passes, `.pyi` emitted, `jaunt check` exits 0; a MIXED module (module-style spec + decorated spec + handwritten helper) builds; after build, importing the governed module and calling a spec function returns the mocked implementation's result (runtime path proven against real build artifacts).

- [ ] **Step 1: Failing tests** for all three surfaces (specs JSON origin+kwargs; init scaffold classifies exactly one spec via `scan_module_source` and `jaunt build --json` on a fresh scaffold with the mocked backend reports it generated; e2e battery above).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** the `cmd_specs` and `init_template.py` changes.
- [ ] **Step 4: Run tests to verify pass; full suite.**
- [ ] **Step 5: Lint, typecheck, commit.** `git commit -m "feat: specs origin surface, magic_module init scaffold, mocked e2e coverage"`.

---

### Task 8: README + in-repo docs (agent-facing)

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md` (Key Concepts + Quick Reference)
- Modify: `src/jaunt/instructions/primer.md`
- Modify: `src/jaunt/prompts/codex_preamble.md` ONLY IF it names decorator syntax in a way module mode contradicts (read it first; content changes restale user builds — spec'd fingerprint behavior, do not hack around it; if a change is needed it ships in a minor release, which this is).

**Interfaces:** none (prose). Writer agents MUST load `/home/diwank/github.com/creatorrr/jaunt/.claude/skills/natural-writing/` first.

Content requirements (spec §1, §4 "new primary style"):

- **README.md:** the leading example becomes magic_module style (module call + docstring-only class + `...` function stub + handwritten helper coexisting); a short "precision layer" paragraph keeps `@jaunt.magic` for per-symbol `deps=`/`prompt=` overrides and decorated-symbol opt-in; feature bullet list gains module-level magic; all stub bodies in README standardize on `...` (drop `raise RuntimeError("spec stub")` idioms).
- **README.md major-feature highlights (user-requested):** the feature list must prominently surface two existing strengths alongside magic_module, stated concretely (all claims verified against `builder.py`/`digest.py` — do not soften or inflate them):
  1. **Parallel, DAG-scheduled builds** — modules are scheduled over the dependency graph with a critical-path-first ready queue: a module starts generating the instant its dependencies finish (no wave barriers), up to `[build] jobs` concurrent workers; a failed module skips only its dependents, the rest of the graph keeps building.
  2. **Smart DAG-based change detection** — SHA-256 digests over AST-normalized contracts, so reformatting, comment edits, and quote-style churn never trigger a rebuild; staleness is dependency-aware (changing a module's exported API restales its dependents, a body-only rebuild does not); and the semantic gate re-freezes behaviorally-equivalent docstring edits instead of paying for a full rebuild.
- **CLAUDE.md:** Key Concepts gains a `magic_module` bullet covering: classification table in one breath (strict stub forms; non-jaunt decorators never governed; jaunt decorators win), module-default kwarg merge (per-symbol wins per key), two-phase registration, the import-time-capture rule (module-level consumption of specs belongs in functions), and digest neutrality iff body form unchanged.
- **primer.md:** the project-aware agent primer teaches module style first, decorators second; stub-body convention `...`.

- [ ] **Step 1: Read all four files fully; draft edits.**
- [ ] **Step 2: Apply; verify** `uv run pytest` still green (primer/template content is asserted in tests — fix assertions that pin old wording ONLY by updating them to the new wording, never by weakening them).
- [ ] **Step 3: Lint (ruff not applicable to md; run anyway for the repo), commit.** `git commit -m "docs: README, CLAUDE.md, primer — magic_module as the primary authoring style"`.

---

### Task 9: docs-site thorough sweep

**Files (all under `docs-site/`):**
- Modify: `app/(home)/page.tsx` (landing hero code sample → magic_module style)
- Modify: `content/docs/index.mdx`, `content/docs/tutorials/quickstart.mdx`, `content/docs/guides/writing-magic-specs.mdx`, `content/docs/concepts/how-jaunt-works.mdx`, `content/docs/concepts/change-detection.mdx`, `content/docs/reference/cli.mdx`, `content/docs/reference/limitations.mdx`, `content/docs/reference/upgrading.mdx`
- Check-and-update if they show decorator-first examples: `content/docs/tutorials/adopt-existing.mdx`, `content/docs/tutorials/whole-class-daemon.mdx`, `content/docs/guides/spec-tips.mdx`, `content/docs/guides/dependencies.mdx`, `content/docs/guides/coding-agents.mdx`
- Leave alone: every page whose frontmatter title is `Moved` (redirect stubs); `/llms.txt` regenerates automatically from page content.

**Interfaces:** none (prose). Writer agents MUST load the natural-writing skill. Keep the Diátaxis-lite voice established in PR #64: tutorials show executed reality, guides are task-oriented, concepts explain, reference is versionless except `upgrading.mdx`.

Content requirements:

- **Landing (`page.tsx` + `index.mdx`):** hero sample becomes the magic_module email-parser example (spec §1's exact shape); decorators appear as the second beat ("need per-symbol control? decorate just that symbol"). The landing feature cards/sections must prominently highlight **parallel DAG-scheduled builds** and **smart DAG-based change detection** as major features (user-requested), with the same verified claims as Task 8's README bullets: critical-path-first ready queue, modules start the instant their deps finish, up to `[build] jobs` workers, failures skip only dependents; AST-normalized digests (formatting never rebuilds), dependency-aware restaling, semantic-gate re-freeze. Card copy links to `/docs/concepts/change-detection` and the parallel-builds section below.
- **`tutorials/quickstart.mdx`:** converts to module style END-TO-END. This tutorial claims executed output — re-execute it against the local checkout (`uv run --project <repo> jaunt build`; the codex CLI is authenticated on this machine) and paste real output. If execution is impossible in the worktree, keep the old decorator quickstart untouched and note the blocker in the task report instead of faking output.
- **`guides/writing-magic-specs.mdx`:** restructure to lead with module style: the classification table from spec §1 rendered as a docs table; the stub-form list; the capture rule ("module-level consumption of specs belongs in functions" + the two warning texts); `@jaunt.preserve` as the intentionally-empty opt-out; decorators as the precision layer (per-symbol kwargs, decorated-symbol opt-in, whole-class three-tier vocabulary unchanged).
- **`concepts/how-jaunt-works.mdx`:** add the two-phase registration story (AST-register at call time → post-import backfill) and Approach-A forwarding (intercept once, rebind, vanish; `__class__` swap-back). Also add a **"Parallel builds"** section (user-requested) explaining the scheduler as it actually works (`builder.py`): the stale-module DAG gets indegrees and a critical-path-length priority; a ready heap seeds with zero-indegree modules; `asyncio.wait(FIRST_COMPLETED)` pops the next module the instant a slot frees (in-flight capped at `[build] jobs`); completing a module immediately enqueues any dependent whose deps are all done; a failure marks only its dependents failed and the rest of the graph keeps building. No level-synchronous batches.
- **`concepts/change-detection.mdx`:** digest neutrality iff body form unchanged; `raise RuntimeError` bodies restale on conversion; module `prompt=` edits restale every governed spec (correct, it's their contract). Add a short **"Does this save tokens?"** subsection (user-requested framing, keep it honest): *yes, potentially a lot — but it depends on when you adopted it and your specific codebase.* Concretely: unchanged modules skip the model entirely; formatting/comment churn never rebuilds; behaviorally-equivalent docstring edits re-freeze via the small `[semantic_gate]` judge instead of a full gpt-5.5 rebuild. What it depends on: adoption timing (the first build after adoption regenerates everything regardless) and codebase shape (dependency fan-out — an exported-API change restales every dependent; many small independent modules amortize best). No unqualified cost claims.
- **`reference/cli.mdx`:** `jaunt specs` origin marker + JSON `"origin"`/`"kwargs"` keys.
- **`reference/limitations.mdx`:** import-time capture (subclass/call/instantiate at module level sees the pre-rebind stub; pickle caveat), circular imports bypass resolution, conditional defs not governed, nested defs out of scope, `magic_module` in `__init__.py` not yet supported (spec §8).
- **`reference/upgrading.mdx`:** new `## 1.4.0` section ABOVE 1.3.1: converting decorator→module style is digest-neutral iff the stub body form is unchanged (the decorator line never entered the digest); `raise RuntimeError("spec stub")` bodies restale once when rewritten to `...`; adding module-level kwargs restales governed specs as any contract change does; `jaunt eject` note — eject, don't hand-strip `@contract` markers in governed modules (a hand-stripped stub-shaped function silently flips to module-magic, spec §7).

- [ ] **Step 1: Read every listed page; draft the sweep.**
- [ ] **Step 2: Apply; build the site** (`cd docs-site && npm run build` or the package.json equivalent) — zero broken links/build errors.
- [ ] **Step 3: Commit.** `git commit -m "docs: docs-site sweep — magic_module as the primary style (landing, quickstart, guides, concepts, reference)"`.

---

### Task 10: Integration gate + release prep

**Files:**
- Modify: `pyproject.toml` (version → `1.4.0`)
- No other source changes expected; fix-forward anything the gate surfaces.

- [ ] **Step 1: Full verification.** `uv run pytest` (expect ~1200+ tests, all green), `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`.
- [ ] **Step 2: Spec cross-check.** Walk spec §1–§7 section by section and point each requirement at a commit in this branch; §6's test list must map 1:1 to test files from Tasks 1, 3, 4, 5, 6, 7. Fix gaps in place.
- [ ] **Step 3: Fresh-scaffold smoke.** In a temp dir: `jaunt init`, then `uv run jaunt specs --json` shows the starter spec with `"origin": "module"`; `uv run jaunt check` exits 4 (unbuilt) with the module listed.
- [ ] **Step 4: Version bump.** Set `version = "1.4.0"` in `pyproject.toml` (release workflow triggers only on main pushes touching this file — the bump MUST ride this PR). `git commit -m "chore: bump version to 1.4.0"`.
- [ ] **Step 5: Push branch; open PR** titled `feat: 1.4.0 — jaunt.magic_module, module-level magic as the primary style` with a body that maps clusters→commits and calls out the two spec deviations (parse cache; origin JSON values).

---

## Post-merge (main session, NOT the workflow)

After the PR merges and PyPI publishes 1.4.0:

1. Update `~/github.com/julep-ai/mem-mcp-b/FEEDBACK-REPLY.md` with a 1.4.0 section: `magic_module` shipped (absorbs finding 22's module-level prompt channel properly — module kwargs replace duplicated per-decorator `prompt=`), migration notes (conversion digest-neutral iff body form unchanged; `raise RuntimeError` stubs restale once; decorated symbols inherit module defaults), and status of the remaining 1.4 backlog items (findings 19 skills-context-budget and 20 frontmatter-dupe — not in this release).
2. Update memory (`MEMORY.md` + `jaunt-1-4-candidates.md`): magic_module shipped; module-level-prompt-channel candidate closed.
