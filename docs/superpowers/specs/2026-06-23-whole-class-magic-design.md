# Whole-class `@jaunt.magic` — Design

**Date:** 2026-06-23
**Status:** Approved design (pre-implementation)
**Topic:** Promote whole-class `@magic` to a first-class, tested authoring mode.

## Problem & motivation

Jaunt's fully-supported pattern today is **per-method `@magic`**: you hand-write a
class and decorate each method to be generated. Decorating the *class itself* with
`@jaunt.magic()` already half-works — the runtime substitutes the generated class
at import time (`runtime.py:215-242`) and the builder rejects mixing whole-class
with per-method `@magic` on the same class (`builder.py:781-787`) — but it is a
**secondary, untested path**: no example uses it, the build prompt has no
whole-class instructions (`prompts/build_module.md:14` only describes per-method
generation), and validation only checks that a class of the right name exists.

This design promotes whole-class `@magic` to a first-class authoring mode so a
user can write a class once, decorate it once, and have Jaunt generate the entire
class body — inventing internals freely while honoring an explicit contract.

## Goals

- One decorator on the `class` statement supports three authoring styles.
- Jaunt may design private helpers and shared state coherently (single-shot).
- The inheritance chain (MRO, overrides/overloads, ABC abstractmethods) is
  considered during generation.
- A clear, loose-but-real validation contract catches drift without being brittle.
- Per-method `@magic` is untouched and remains fully supported.

## Non-goals (v1)

- Recursive generation *into* nested classes (a nested class is preserved
  verbatim, or decorated separately on its own).
- Custom metaclasses (already rejected, `runtime.py:217`).
- Replacing or deprecating per-method `@magic`.

## 1. Authoring model & mode detection

A single `@jaunt.magic()` on a `class` statement. Three modes are **auto-detected
from the class AST** — no new decorator kwarg:

- **docstring-only / invented-API** — body is just a docstring (optionally `...`
  or `pass`) with no member definitions. Jaunt invents the full public API from
  the docstring + base classes.
- **stubs** — body has method definitions whose bodies are *all* empty-ish stubs.
  Jaunt implements them against their declared signatures.
- **mix** — some stub methods, some real methods/attributes. Jaunt implements
  stubs, preserves the rest, and may add private helpers.

Mode is descriptive, not a switch the user sets: the prompt and validator adapt to
whatever the body contains.

### Stub heuristic (the single rule, applied everywhere)

A method or property is a **stub** iff its body consists *only* of a combination
of:

- a docstring expression,
- `...` (`Ellipsis`),
- `pass`,
- `raise NotImplementedError` / `raise NotImplementedError(...)`.

Any other statement ⇒ the method is **preserved verbatim**. The same rule applies
uniformly to `__init__`, `@property` (getter/setter), `@classmethod`, and
`@staticmethod`. The member's decorators are always preserved regardless of
stub-vs-real status.

### Explicit override: `@jaunt.preserve`

The heuristic has one blind spot — a method whose *intended* real implementation
is itself empty-ish (e.g. an intentional `def __init__(self): pass`, a no-op hook
`def on_event(self): pass`, a deliberately-abstract `raise NotImplementedError`,
or a `...` placeholder). The `@jaunt.preserve` decorator is the explicit escape
hatch: it forces a method into the **preserved-verbatim** set, overriding the
body-shape heuristic.

Semantics:

- **Build-time directive only.** The builder reads it from the AST (as it already
  reads `@magic`); an explicit `@preserve` always wins over the heuristic.
- **Runtime no-op.** It is an identity decorator so the spec module imports
  cleanly. (At runtime the whole class is substituted by the generated class, so
  the decorator has no runtime role.)
- **Stripped from the generated output.** The generated class contains the method
  *without* `@jaunt.preserve`, but *with* its real decorators (`@property`,
  `@classmethod`, …). The preserved-intact check compares against the stripped
  form.
- **Not `@magic`.** It does not trip the whole-class-vs-per-method conflict rule —
  it is a complementary annotation.
- **Scope.** Meaningful only inside a whole-class `@magic`. Elsewhere (per-method
  mode or plain classes, where methods are already preserved) it is a harmless
  no-op and emits a lint-style warning.
- **Form.** Bare `@jaunt.preserve` is the primary form; `@jaunt.preserve()` is
  also accepted. Takes no arguments.

There is intentionally **no** mirror "(re)generate this real-bodied method"
marker (YAGNI): to regenerate a method, reduce its body to a stub and the
heuristic handles it. The mental model stays simple — the heuristic decides,
`@preserve` is the one override.

### Relationship to per-method `@magic`

Per-method mode is unchanged. The existing mutual-exclusivity rule stays: a class
may not carry both whole-class `@magic` and per-method `@magic`
(`builder.py:781-787`). Within whole-class mode, stub methods are decorator-free,
so the conflict never arises.

## 2. The contract — preserve vs. generate

**Preserved (must appear in the generated class):**

- Real (non-stub) methods, plus any method marked `@jaunt.preserve` regardless of
  its body shape — preserved *verbatim* (AST-equivalent, with `@jaunt.preserve`
  itself stripped from the comparison and the output).
- Class attribute assignments and annotations.
- Base classes.
- The class's own decorators (`@dataclass`, `@runtime_checkable`, …).
- The class docstring's content — **retained**, but the LLM **may append
  additional notes** (e.g. per-method documentation). The original text must
  remain present; additions are allowed.

**Generated:**

- Stub method bodies, matching their declared signatures (signatures are strong
  hints — may widen compatibly; dropping a declared param is discouraged and
  surfaces as a warning, not a hard failure).
- Any private/helper methods and shared internal state Jaunt deems necessary
  (always allowed; never penalized).
- In docstring-only mode, the entire public surface.

**Inheritance is first-class.** The builder resolves base classes — project specs
via `module_api` summaries, external classes via `inspect` — and feeds the prompt:

- public/overridable method signatures of bases,
- ABC `@abstractmethod`s that must be implemented,
- enough context to generate overrides/overloads against the base contract.

## 3. Generation (single-shot — "Approach B")

A new builder branch handles class specs (decorated `obj` is a `type`,
`class_name is None`, qualname has no dot). The class is **one spec → one LLM
call** that emits the whole class.

Build context assembled for the call:

- the spec class source,
- the parsed stub-vs-preserved split,
- a resolved **base-class / MRO contract block**,
- transitive dependency API summaries,
- any `# Decorator prompt` section.

The build prompt (`prompts/build_module.md`) gains a whole-class section: emit the
complete class named `X`; implement every stub against its signature + docstring;
keep all preserved methods (heuristic-detected or `@jaunt.preserve`-marked, the
latter emitted *without* the `@jaunt.preserve` decorator), attributes, base
classes, and class decorators; you
may add private helpers and shared state; honor the inheritance contract and
implement all inherited abstractmethods; in docstring-only mode design the full
public API from the docstring; retain the docstring content (you may append
notes).

Output is the whole class, written into the generated module exactly as today.

## 4. Validation

A class-aware validator in `validation.py`, used when the expected name is a
whole-class spec. Severity levels:

**Fail the build:**

- **Structure:** every declared stub method is defined in the generated class;
  declared base classes are preserved (by name); the class's own decorators are
  preserved.
- **Abstractmethods:** every inherited ABC `@abstractmethod` is implemented. (An
  unimplemented abstractmethod yields an uninstantiable class — a real bug.)
- **Preserved-intact:** each verbatim method and attribute — heuristic-detected
  *or* `@jaunt.preserve`-marked — is AST-equivalent (formatting-normalized) to the
  spec, with `@jaunt.preserve` stripped before comparison. Catches LLM drift on
  parts that must not change.
- **Docstring retained:** the spec docstring's original content is present in the
  generated class docstring (additions allowed; removal/rewrite fails).

**Warn only (never fail):**

- A stub's generated signature differs from the declared one — warn if it *drops*
  a declared parameter; otherwise silent.

**Always allowed:** extra private or public methods, extra internal state,
expanded docstrings.

## 5. Incremental builds, dependencies, runtime

- **Digest:** the whole class is one spec with one digest over: class source +
  decorator kwargs + transitive dependency APIs + **resolved base-class API**.
  Base-class changes invalidate the subclass.
- **Dependents & tests:** `module_api.py` already emits `kind="class"` summaries
  with members; these feed dependents and `@jaunt.test` targeting. In
  docstring-only mode, dependents/tests see the *generated* class's API summary.
- **Invented-API churn is bounded:** an unchanged docstring ⇒ stable digest ⇒ no
  regeneration ⇒ stable API.
- **Dependency graph:** a class is a single node (already true in `deps.py`). Base
  classes that are project specs become dependencies so they build first and
  invalidate downstream.
- **Runtime:** the only addition is a `@jaunt.preserve` identity decorator
  (no-op, returns the function unchanged) exported from the `jaunt` package.
  Import-time substitution of the class is otherwise unchanged
  (`runtime.py:215-242`); the not-built fallback already raises an actionable
  error.

## 6. Deliverables (file-by-file)

- `prompts/build_module.md` — whole-class generation section.
- `builder.py` — whole-class spec branch; assemble base-class/MRO contract block;
  wire base-class API into the digest.
- `validation.py` — class-aware validator (structure, abstractmethods,
  preserved-intact, docstring-retained, loose-signature warnings).
- `runtime.py` + `jaunt/__init__.py` — add and export the `@jaunt.preserve`
  identity decorator. The "used outside a whole-class `@magic`" warning is emitted
  during build-time analysis (not at runtime, where the enclosing class's
  decoration isn't yet known).
- New util (e.g. in `decorator_analysis.py` or a small new module) — mode
  detection + stub heuristic (with `@jaunt.preserve` override) + stub/preserved
  split + base-class resolution.
- `module_api.py` / `digest.py` — ensure base-class API participates in the
  class digest.
- `examples/06_whole_class/` — a runnable example covering stubs, mix, and
  docstring-only.
- Tests — mode detection, stub heuristic, generation (per mode), validation
  (structure / drift / abstractmethods / inheritance / docstring), digest
  invalidation on base-class change.
- Docs — `CLAUDE.md` and the `jaunt` skill: document whole-class mode.

## 7. Testing plan

- **Unit:** stub heuristic across `def`/`async def`/`property`/`classmethod`/
  `staticmethod` and each empty-ish body shape; mode detection for the three
  modes; base-class resolution for project-spec and external bases;
  `@jaunt.preserve` forces preservation of an empty-ish-bodied method and is
  stripped from the output; `@jaunt.preserve` outside a whole-class `@magic`
  warns and is a no-op.
- **Validation:** missing stub method (fail); dropped base class (fail);
  unimplemented abstractmethod (fail); drifted preserved method (fail); rewritten
  docstring (fail) vs appended notes (pass); dropped-param signature (warn);
  extra private methods (pass).
- **Builder integration (mocked LLM):** generate a class in each mode end-to-end;
  confirm expected_names, written output, and digest behavior.
- **Incremental:** changing a base spec marks the subclass stale; unchanged
  docstring-only spec does not regenerate.

## 8. Risks & mitigations

- **LLM drift on preserved parts** → preserved-intact AST check fails the build.
- **Invented-API instability across builds** → bounded by digest stability;
  documented so users know docstring-only trades control for magic.
- **Over-strict validation causing false failures** → signatures are loose
  (warn-only); only structure/preservation/abstractmethods fail.
- **Large prompt/output for big classes** → acceptable in v1; single-shot is
  required for coherent internals. Revisit if it becomes a problem.
