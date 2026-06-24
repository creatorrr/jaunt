# Whole-class `@jaunt.magic` under the default aider engine — Design

**Date:** 2026-06-24
**Status:** Approved design (pre-implementation)
**Branch:** `feat/whole-class-magic`
**Topic:** Make whole-class `@magic` generation work reliably with the default
`aider` engine by feeding aider a carefully crafted, intensive scaffold + contract
as input, with a whole-file → legacy reliability ladder.

## Problem & motivation

Whole-class `@magic` generation (the docstring-only / stubs / mix authoring modes)
was implemented against the direct `GeneratorBackend` and is fully unit-tested,
but it **fails under the project's default `aider` engine**. The example
`examples/06_whole_class` currently pins `[agent] engine = "legacy"` as a
workaround (see `aider-whole-class-gap` memory).

Root cause: for a whole-class build the aider backend seeds the target file
**empty** (`AiderGeneratorBackend.generate_module` → `_plan_attempt(...,
failure_kind=None)` returns `target_content=""`, `aider_backend.py:446`) and asks
aider to emit the entire class via **diff / SEARCH-REPLACE** (architect →
`editor-diff`). Diffing a large, complete class body against an empty file is
unreliable: aider applies partial edits, the top-level class never lands, and
`validate_generated_source` reports "Missing top-level definition". The retry path
(`_plan_attempt` with a failure kind) only escalates to whole-file editing on an
`edit_apply` error — a "missing class" *contract* failure keeps diff mode — so the
two attempts exhaust and the build fails.

Two facts make a clean fix possible:
- The executor **writes `task.target_file.content` to disk before invoking aider**
  (`aider_executor.py:144-146`), so seeding a scaffold gives aider real anchors.
- Whole-class context is already half-built: `base_contract_block` is plumbed into
  the aider contract (`aider_backend.py:89`), and `class_analysis` provides the
  stub/preserved split (`split_class_members`) and base contract
  (`resolve_base_contract`).

## Goals

- `jaunt build` and `jaunt test` succeed for whole-class specs under the **default
  aider engine**, across all three modes (docstring-only / stubs / mix).
- Preserved methods (real + `@jaunt.preserve`) survive verbatim; the class
  validator's existing guarantees still hold.
- No behavior change (and no regression risk) for function-only and
  per-method-class modules.

## Non-goals

- Changing the direct/legacy backend behavior.
- Per-method `@magic` class generation under aider (out of scope; unchanged).
- Bumping reasoning effort/compute — the "intensive" lever here is *context*, not
  extra compute (easy to add later if desired).

## 1. Scaffold builder (the intensive input)

A new function in `class_analysis.py` derives a **scaffold** used as the aider
target file's starting content (instead of `""`). For a whole-class spec it emits:

- the spec module's **import statements** — *all* top-level `import` / `from …
  import` nodes parsed from the spec module (not just `extract_spec_preamble`,
  which stops at the first decorated def and would miss imports a preserved method
  or class decorator needs). The contract also tells aider it may add imports.
- the class **header** with **base classes and class decorators preserved** (the
  `@magic` decorator stripped),
- the class **docstring** retained,
- class **attribute** assignments/annotations verbatim,
- **preserved methods verbatim** (heuristic-real + `@jaunt.preserve`, with the
  `@jaunt.preserve` decorator stripped), under a `# preserved — do not modify`
  comment,
- each **stub method** as its signature + docstring + a body:
  `raise NotImplementedError("jaunt: implement <Class>.<method> per the spec")  # jaunt:implement`
  where `# jaunt:implement` is a unique sentinel.

Aider then fills each `# jaunt:implement` body via diff against real anchors and
may add `__init__`, private helpers, and shared state.

**Docstring-only mode** has no stubs to anchor: its scaffold is header + docstring
+ `pass`. It relies more on the escalation/legacy rungs below, **and** on the
docstring-only completeness guard in §5 — without that guard a generated
`class C: "..."; pass` would pass validation and ship an empty class (Codex #1).

**Scope = component, not module (Codex #8).** The builder splits a module's
independent specs into parallel *components* and merges the generated sources;
duplicate top-level names are a hard merge error (`builder.py:984`). The scaffold
must therefore cover **only the current component's expected names**
(`component_expected`), never the whole module. A whole-class component scaffolds
the class; a sibling function in a *different* component is scaffolded in its own
component (or left to the existing empty-seed path).

**Gating:** scaffold-seeding activates **only for components whose expected output
includes a whole-class spec**. Function-only and per-method-class components keep
the existing empty-seed path unchanged (no regression risk).

Signature (illustrative):
`build_component_scaffold(*, entries, expected_names, base_contracts, spec_imports) -> str`.

## 2. Rich whole-class contract section

The contract content is **per-component data**, so it cannot come from the static
`aider_contract_addendum(kind)` hook — that is called with no `ctx`
(`aider_backend.py:42`) and cannot name this component's methods (Codex #7).
Instead the **builder** renders a `whole_class_contract_block` from
`class_analysis` and carries it on `ModuleSpecContext`; the aider backend writes it
into a `context/whole_class_contract.md` read-only file (mirroring how
`base_contract_block` is plumbed). It states explicitly:

- which methods to **fill** (the stubs) — "replace each `# jaunt:implement` body",
- which methods are **preserved — must not change**,
- the **base-class / abstractmethod contract** (via `base_contract_block`) —
  implement all inherited abstractmethods; make overrides consistent with base
  signatures,
- the **docstring-retention** rule (retain content; additions allowed),
- "you may add `__init__`, private helpers, and shared state",
- for **docstring-only** specs: "design the full public API the docstring implies."

The static `_AIDER_BUILD_GUIDANCE` is unchanged and still applies.

## 3. Aider backend wiring

`AiderGeneratorBackend` learns, per component, whether it is a whole-class build
from new `ModuleSpecContext` fields the builder populates:

- `seed_target_content: str = ""` — the scaffold (§1). `_plan_attempt(...,
  failure_kind=None)` returns it as `target_content` (not `""`), keeping the first
  attempt **architect + editor-diff** — diff now succeeds because the anchors exist.
- `whole_class_contract_block: str = ""` — the contract section (§2).
- `whole_class: bool` (or derived from the above being non-empty) — gates the
  scaffold seeding, the in-loop validator (§4), and the escalation path.

**Cache key (Codex #6).** `cache_key_from_context` enumerates ctx fields manually
(`cache.py`), so the new fields are invisible to it by default — a pre-scaffold
cached response could be reused and bypass the scaffold path. The cache key (and
the aider `generation_fingerprint`) must incorporate `seed_target_content` +
`whole_class_contract_block` so scaffold-seeded generations get a distinct key and
stale entries don't match. (The scaffold is deterministic from spec source, which
is already in the module digest, so this only affects the LLM-response cache.)

## 4. Reliability ladder

**Prerequisite — the class validator must run *inside* the aider retry loop
(Codex #2).** Today `backend.generate_with_retry(..., extra_validator=...)` is given
the *cheap* `_retry_validator` (handwritten-redefinition + ty only); the
class-aware `validate_build_class_source` runs post-merge in `_validate_module_candidate`
(`builder.py:1405`), **after** `generate_with_retry` has already returned. So a
missing method, dropped base/decorator/attribute, unimplemented abstractmethod,
drifted preserved method, or unfilled stub fails *outside* the loop — no retry, no
escalation. The fix: for whole-class components the builder passes a **class-aware
`extra_validator`** (the structural subset of `validate_build_class_source`, minus
ty) into `generate_with_retry`, and maps its failure to a `failure_kind` that
escalates. Only then can the ladder below actually fire.

1. **Attempt 1:** architect + diff on the seeded scaffold.
2. **Escalate to whole-file:** when the in-loop class validator reports a
   missing-class / unfilled-stub / contract failure, `_plan_attempt` escalates to
   whole-file editing (`editor-whole` in architect, or `code` + `whole`), seeded
   with the scaffold. (Extends today's escalation, which only triggers on
   `edit_apply`.)
3. **Legacy fallback (Codex #4, #5):** the builder cannot construct a direct
   backend itself — `run_build` receives a `GeneratorBackend`, and the
   provider→backend mapping lives in `cli._build_backend`. So a **fallback backend
   is injected**: `cli._build_backend` builds the direct backend for
   `cfg.llm.provider` and passes it to `run_build` (new optional
   `fallback_backend: GeneratorBackend | None`). If the aider path still fails a
   whole-class component, the builder retries that component once with
   `fallback_backend`. To avoid cross-engine incremental corruption:
   - the fallback write **bypasses the aider response cache** (no read, no write),
   - the output header is **stamped with the fallback backend's own
     generation fingerprint/provider**, so `status` and future cache lookups treat
     the module as belonging to the engine that actually produced it,
   - the fallback is logged.

## 5. Validation guards

Extend `validate_build_class_source` (and reuse it as the in-loop validator, §4)
with three new checks:

- **Unfilled-stub detection — AST, not text (Codex #3).** A text search for the
  `# jaunt:implement` sentinel is insufficient: aider can replace the body with a
  bare `raise NotImplementedError` or `...` and strip the comment, passing a text
  check while the body is still a stub. The authoritative check: for each
  **declared stub method**, the generated method's body must NOT satisfy
  `class_analysis.is_stub_body` (i.e. it was actually implemented). The sentinel
  removal is kept only as a secondary signal.
- **Docstring-only completeness (Codex #1).** When the spec class declares no stub
  methods (docstring-only mode), require the generated class to be non-trivial:
  at least one public method defined (a `pass`-only / docstring-only body fails).
  Without this, an empty generated class passes and ships silently.
- **Class-attribute preservation (Codex #9).** Each class attribute declared in the
  spec (name + annotation/value) must be present in the generated class. The spec
  promises attributes survive verbatim, but nothing currently enforces it; aider
  whole-file rewrites could drop `CAPACITY: int = 10`.

## 6. Deliverables (file-by-file)

- `class_analysis.py` — `build_component_scaffold(...)` (class scaffold + sibling
  function stubs), reusing `split_class_members` / `resolve_base_contract`; a
  helper to collect the spec module's full top-level imports; a
  `whole_class_contract_block` renderer (or this lives in builder).
- `generate/base.py` — add `seed_target_content: str = ""`,
  `whole_class_contract_block: str = ""`, and a whole-class flag to
  `ModuleSpecContext`.
- `cache.py` — include the new ctx fields in `cache_key_from_context` (Codex #6).
- `generate/aider_backend.py` — seed `target_content` from `ctx.seed_target_content`;
  write `whole_class_contract_block` as a `context/` read-only file; include the new
  fields in `generation_fingerprint`; extend `_plan_attempt` escalation to
  whole-file on missing-class/contract failures.
- `builder.py` — per-**component** scaffold + contract block; pass the **class-aware
  `extra_validator`** into `backend.generate_with_retry` for whole-class components
  (Codex #2); orchestrate the **`fallback_backend`** retry with cache-bypass and
  fallback-engine header stamping (Codex #4, #5); new optional
  `run_build(..., fallback_backend=None)` param.
- `cli.py` — `_build_backend` builds the direct backend for `cfg.llm.provider` and
  passes it as `fallback_backend` to `run_build` when the engine is aider.
- `validation.py` — AST unfilled-stub check, docstring-only completeness check,
  class-attribute preservation check (§5); structural subset usable as the in-loop
  validator.
- `examples/06_whole_class/jaunt.toml` — **remove** the `[agent] engine = "legacy"`
  pin (back to default aider); `README.md` — drop the "needs legacy" note.
- `CLAUDE.md` / jaunt skill — update if they mention the legacy requirement.
- Update the `aider-whole-class-gap` memory once this lands (gap closed).

## 7. Testing plan

- **Unit:**
  - scaffold builder: stubs → `# jaunt:implement` bodies; preserved methods →
    verbatim (with `@jaunt.preserve` stripped); bases, class decorators, docstring,
    and class attributes retained; docstring-only → header + docstring + `pass`;
    imports collected from the full spec module (not just the preamble).
  - per-component scope (Codex #8): a module that splits into a whole-class
    component + a function component scaffolds each independently with no duplicate
    top-level names after merge.
  - gating: function-only / per-method components produce no scaffold (empty seed).
  - in-loop validator (Codex #2): a class-aware `extra_validator` fed to a stub
    `generate_with_retry` triggers a second attempt / escalation on a missing-method
    or unfilled-stub failure (not just post-merge).
  - validation guards (§5): AST unfilled-stub (bare `raise`/`...` with sentinel
    stripped still fails); docstring-only empty class fails, non-trivial passes;
    dropped class attribute fails.
  - cache key (Codex #6): two ctxs differing only in `seed_target_content` produce
    different `cache_key_from_context` keys.
  - fallback stamping (Codex #5): a simulated fallback writes the direct backend's
    fingerprint and does not read/write the aider response cache.
  - contract block: `whole_class_contract_block` rendered for whole-class
    components and surfaced to aider as a context file.
- **End-to-end (real LLM via Anthropic, default aider engine):** build
  `examples/06_whole_class` — all three modes generate; `@preserve is_empty`
  verbatim; `jaunt test` baseline suite for `TempStats` passes. Confirm the
  legacy-engine pin is no longer required.

## 8. Risks & mitigations

- **Docstring-only still thin for diff** → escalation to whole-file, then legacy
  fallback; the docstring-only completeness guard (§5) prevents an empty class from
  silently passing.
- **Scaffold seeding regressing function/method paths** → strictly gated on
  *components* containing a whole-class spec; existing paths untouched.
- **Aider drifting preserved methods/attributes during whole-file rewrite** → the
  class validator's preserved-intact + attribute + AST-stub checks fail the build,
  triggering the next rung.
- **Legacy fallback diverging from aider config / corrupting incremental state** →
  built from the same `cfg.llm.provider`, injected via `cli._build_backend`; the
  fallback write bypasses the aider cache and is stamped with the fallback engine's
  fingerprint so `status`/cache stay honest; logged.
- **In-loop validator slowing the common path** → the in-loop `extra_validator` is
  the *structural* subset (no ty), so it is cheap; ty runs once at the post-merge
  candidate stage as today.

## 9. Codex review incorporated

This design was reviewed by Codex (read-only consult, 2026-06-24). All ten findings
were verified against the code and folded in: the three structural ones (#2 in-loop
validator, #4 fallback injection, #8 per-component scaffold) were confirmed by
reading `builder.py:1148-1151`, `1405`, `984` and `cli._build_backend`, and reshaped
§3/§4/§1; #1/#3/#9 hardened §5; #5/#6 added cache/fingerprint/stamping rules;
#7/#10 adjusted the contract-rendering and import-collection mechanics.
