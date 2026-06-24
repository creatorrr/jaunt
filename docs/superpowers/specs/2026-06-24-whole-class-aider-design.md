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

A new function in `class_analysis.py` derives a **module scaffold** used as the
aider target file's starting content (instead of `""`). For a whole-class spec it
emits:

- the spec module's **import preamble** (reuse `module_contract.extract_spec_preamble`),
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
+ `pass`. It relies more on the escalation/legacy rungs below (an accepted
limitation — there is nothing to anchor a diff against).

**Gating:** scaffold-seeding activates **only for modules whose expected output
includes a whole-class spec**. Function-only and per-method-class modules keep the
existing empty-seed path unchanged (no regression risk).

Signature (illustrative):
`build_module_scaffold(*, entries, expected_names, base_contracts) -> str` —
emits a scaffold covering all expected top-level symbols of the module; whole-class
specs get the class scaffold above, sibling function specs get signature +
`# jaunt:implement` stub bodies so a mixed module still seeds completely.

## 2. Rich whole-class contract section

A new section in `generate/aider_contract.py` (rendered into
`context/contract.md`) that, for whole-class builds, states explicitly:

- which methods to **fill** (the stubs) — "replace each `# jaunt:implement` body",
- which methods are **preserved — must not change**,
- the **base-class / abstractmethod contract** (already available via
  `base_contract_block`) — implement all inherited abstractmethods; make overrides
  consistent with base signatures,
- the **docstring-retention** rule (retain content; additions allowed),
- "you may add `__init__`, private helpers, and shared state",
- for **docstring-only** specs: "design the full public API the docstring implies."

This composes with the existing `_AIDER_BUILD_GUIDANCE`.

## 3. Aider backend wiring

`AiderGeneratorBackend` learns, per module, whether it is a whole-class build
(from `ctx`). When it is:

- `_plan_attempt(..., failure_kind=None)` returns the **scaffold** as
  `target_content` (not `""`), keeping the first attempt **architect + editor-diff**
  — diff now succeeds because the anchors exist.
- The scaffold is computed once and carried on `ModuleSpecContext` (new optional
  field, e.g. `seed_target_content: str = ""`, populated by the builder), so the
  backend stays a thin consumer and the builder owns spec analysis.

## 4. Reliability ladder

1. **Attempt 1:** architect + diff on the seeded scaffold.
2. **Escalate to whole-file:** on a missing-class / unfilled-sentinel / contract
   failure for a whole-class module, `_plan_attempt` escalates to whole-file
   editing (`editor-whole` in architect, or `code` + `whole`), seeded with the
   scaffold. (Extends today's escalation, which only triggers on `edit_apply`.)
3. **Legacy fallback:** if aider still fails validation for a whole-class module,
   the **builder** retries that module once with the direct backend built from
   `cfg.llm.provider` (openai/anthropic/cerebras), which already generates whole
   classes reliably. The fallback is logged.

## 5. Validation guard

Extend the class validator (or the generated-source validation for whole-class)
to **fail if any `# jaunt:implement` sentinel remains** in the output — catching
aider leaving a stub unfilled. The sentinel is unique, so legitimate
`NotImplementedError` (e.g. in a preserved method) is unaffected.

## 6. Deliverables (file-by-file)

- `class_analysis.py` — `build_module_scaffold(...)` (+ helper to render a class
  scaffold and a function-stub scaffold), reusing `split_class_members` /
  `resolve_base_contract`.
- `module_contract.py` — reuse `extract_spec_preamble` for the scaffold imports.
- `generate/base.py` — add `seed_target_content: str = ""` to `ModuleSpecContext`.
- `generate/aider_contract.py` — whole-class contract section.
- `generate/aider_backend.py` — seed `target_content` from `ctx.seed_target_content`
  for whole-class builds; extend `_plan_attempt` escalation to whole-file on
  missing-class/contract failures.
- `builder.py` — compute the scaffold for whole-class modules and set
  `seed_target_content`; legacy-backend fallback for a whole-class module that
  fails under aider.
- `validation.py` — sentinel-remaining guard.
- `examples/06_whole_class/jaunt.toml` — **remove** the `[agent] engine = "legacy"`
  pin (back to default aider); `README.md` — drop the "needs legacy" note.
- `CLAUDE.md` / jaunt skill — update if they mention the legacy requirement.
- Update the `aider-whole-class-gap` memory once this lands (gap closed).

## 7. Testing plan

- **Unit:**
  - scaffold builder: stubs → `# jaunt:implement` bodies; preserved methods →
    verbatim (with `@jaunt.preserve` stripped); bases, class decorators, docstring,
    and class attributes retained; docstring-only → header + docstring + `pass`;
    mixed module (class + sibling function) → both scaffolded.
  - gating: function-only / per-method modules produce no scaffold (empty seed).
  - sentinel guard: output retaining `# jaunt:implement` fails validation; filled
    output passes.
  - contract section: whole-class contract text present for whole-class builds.
- **End-to-end (real LLM via Anthropic, default aider engine):** build
  `examples/06_whole_class` — all three modes generate; `@preserve is_empty`
  verbatim; `jaunt test` baseline suite for `TempStats` passes. Confirm the
  legacy-engine pin is no longer required.

## 8. Risks & mitigations

- **Docstring-only still thin for diff** → escalation to whole-file, then legacy
  fallback guarantees completion; documented limitation.
- **Scaffold seeding regressing function/method modules** → strictly gated on
  modules containing a whole-class spec; existing paths untouched.
- **Aider drifting preserved methods during whole-file rewrite** → the class
  validator's preserved-intact + sentinel checks fail the build, triggering the
  next rung.
- **Legacy fallback diverging from aider config** → built from the same
  `cfg.llm.provider`; logged so the engine switch is visible.
