# Class interface guideposts + inheritance-aware whole-class `@magic` — Design

**Date:** 2026-07-03
**Status:** Approved design (pre-implementation)
**Topic:** Make declared method stubs an explicit three-tier interface contract, and make
whole-class generation genuinely inheritance-aware (build ordering, fresh superclass
context, composition guidance, staleness propagation).

## Problem & motivation

Whole-class `@jaunt.magic` (spec: `2026-06-23-whole-class-magic-design.md`) supports
docstring-only, stubs, and mixed authoring, with `@jaunt.preserve` as the hand-written
escape hatch. Two things are still implicit or broken:

1. **Interface flexibility is undocumented and unenforceable.** Declared stub
   signatures are *soft* today — the generator must implement each declared stub, but
   signature drift is warn-only (`validation.py` `class_build_warnings`) and extra
   methods are silently allowed. There is no way to say "this signature is load-bearing;
   implement exactly this" versus "this stub is a sketch; adapt it." Users who publish a
   class's interface to external callers have no lock; users who want Jaunt to design
   freely get no explicit permission either.

2. **Inheritance is second-class in practice.** `class B(A)` where both are specs only
   creates a dependency edge via `auto_deps` (`runtime.py:236-247`), which is **gated
   behind `infer_deps`** (`deps.py:266-274`). Inference off ⇒ B can build before A, with
   no superclass context. The base-class contract block is derived from the *runtime*
   object frozen at import (`builder.py:660-666` → `resolve_base_contract`), so an
   unbuilt superclass yields a degraded contract and a superclass regenerated earlier in
   the same build is not re-inspected. Nothing ties B's freshness to A's *generated*
   API: change A's interface without touching B's spec and B stays silently stale. And
   the prompt says nothing about composing small methods or building on inherited
   generated methods. There is no end-to-end test of `class B(A)` with both `@magic`.

## Goals

- An explicit, documented **three-tier vocabulary** for a whole-class spec's methods:
  preserved / sealed / guidepost — spelled entirely with existing decorators.
- **Always-on structural base edges**: a spec'd base class named in the class header is
  a dependency, full stop — not gated by inference.
- **Fresh superclass context**: the prompt's inherited-API view of a spec'd base comes
  from its generated artifact on disk, not a stale runtime snapshot.
- **Composition guidance**: every whole-class build is told to prefer small composable
  methods and to build on inherited (including generated) base-class methods.
- **Staleness propagation**: a change to a spec'd base's generated *public API* marks
  subclasses stale; implementation-only rebuilds of the base do not.
- Byte-identical *contract* digests for existing marker-free specs — no digest-driven
  mass rebuild on upgrade (whole-class modules restale once from the changed prompt
  block, §5; function-only projects are untouched).

## Non-goals (v1)

- Method-level `@magic` on hand-written classes and `@jaunt.contract` classes keep
  current behavior; the tier vocabulary does not extend to them.
- Kwargs on an inner `@jaunt.magic` (`deps=`, `test=`, `prompt=`) inside a whole-class
  spec are **rejected**, not half-supported. Per-method deps merging is a possible
  follow-up.
- No override signature-compatibility checking against base classes (LSP-style
  variance analysis) — the model is *informed* of base signatures, not policed.
- No changes to contract mode, nested classes, or metaclass handling.

## 1. The three tiers

On a whole-class `@jaunt.magic` spec, each method belongs to exactly one tier:

| Tier          | Marker            | Body         | Generator may                                                                 |
|---------------|-------------------|--------------|-------------------------------------------------------------------------------|
| **Preserved** | `@jaunt.preserve` | hand-written | nothing — kept verbatim (unchanged behavior)                                   |
| **Sealed**    | `@jaunt.magic`    | stub         | implement the body only; signature (params, defaults, annotations, return) enforced exactly |
| **Guidepost** | none (default)    | stub         | adapt the signature, rename/add parameters, add methods — the docstring intent is the contract |

No new decorator. `@jaunt.magic` on a method *inside* a whole-class spec means the same
thing it means everywhere: "Jaunt implements this, as declared." Standalone method-level
`@magic` (undecorated class) is untouched.

The default tier makes today's implicit softness explicit: unmarked stubs are
guideposts, and the prompt now *says so* (§3), instead of the model guessing how much
liberty it has.

### Absorption at decoration time

Method decorators evaluate before the class decorator. An inner `@jaunt.magic` therefore
first registers as a standalone method spec (the `class_name is not None` branch,
`runtime.py:267-268`). When the class-level `@jaunt.magic` then runs
(`runtime.py:270-297`), it:

1. scans its own members for method-spec wrappers (`__jaunt_spec_ref__` on the wrapper),
2. **unregisters** those method specs from the registry,
3. records the member names as the whole-class spec's `sealed_members`,
4. restores the original undecorated function objects onto the class body it registers
   (so the source AST and the runtime object agree).

If the class is never decorated, the inner specs remain standalone method specs —
existing behavior, zero migration. Because absorption happens at import time, discovery
(`discovery.py`) and `jaunt specs` never see phantom method specs for absorbed members.

On the build side, detection is AST-based and does not depend on the runtime absorption:
`class_analysis.py` gains `is_magic_decorator` (mirror of `is_preserve_decorator`,
`class_analysis.py:39-60`) and `MemberSplit` grows a third bucket:
`MemberSplit(stubs, sealed, preserved, preserve_marked)`. `classify_class_mode` counts
sealed methods as stubs (a class of only sealed methods is still `stubs` mode). The seed
scaffold (`build_class_scaffold`, `class_analysis.py:207-247`) strips inner
`@jaunt.magic` exactly as it strips `@jaunt.preserve`.

### Errors (decoration-time where possible, discovery-time otherwise)

- `@jaunt.magic` + `@jaunt.preserve` stacked on one method → error (contradictory tiers).
- Inner `@jaunt.magic` on a **non-stub** body inside a whole-class spec → error:
  "hand-written body: use `@jaunt.preserve` to keep it, or reduce it to a stub for Jaunt
  to implement."
- Inner `@jaunt.magic(...)` with any kwargs inside a whole-class spec → error (v1).
- The existing wrong-order guard (`@classmethod`/`@staticmethod` above `@magic`,
  `runtime.py:199-207`) applies to inner magic unchanged.

All three surface as config/discovery errors (exit 2), before any model call.

## 2. Dependency graph: always-on base edges

`SpecEntry` (`registry.py:25-39`) gains `base_refs: list[str]` — the project base-class
spec refs `resolve_base_contract` already computes (`class_analysis.py:102-138`) —
recorded **separately** instead of merged into `auto_deps` (today's
`runtime.py:236-247` merge is removed).

`build_spec_graph` (`deps.py:226-379`) applies `base_refs` **unconditionally**, at the
same rank as explicit `deps=` — not gated by `infer_deps` overrides or `infer_default`
(`deps.py:252-274`). Rationale: inheritance is structural fact from the class header,
not an inference; B cannot be planned without A. Consequences:

- A always builds before B; A's generated source/API land in B's dependency context
  (`_collect_dependency_context`, `builder.py:1488-1517`) even with inference off.
- `graph_digest` transitivity (`digest.py:354-384`) always covers the base.
- A base cycle is now always a hard cycle error (exit 2) — correct: it is a real cycle.

## 3. Prompt & context

### Tiered whole-class contract block

`render_whole_class_contract` (`class_analysis.py:250-289`) renders three explicit
method sections instead of two:

- **Sealed** — "implement exactly these signatures; do not rename, add, or remove
  parameters or change annotations."
- **Guideposts** — "these signatures are sketches of intent; you may adapt them
  (parameters, splitting into several methods, additional public methods) as long as the
  documented behavior is delivered."
- **Preserved** — unchanged ("keep verbatim, emit without the decorator").

### Composition guidance (always-on)

A new fixed paragraph in the same block, present for every whole-class build:

> Prefer small, single-purpose methods composed into the public interface over monolithic
> bodies. When a base class provides functionality — including generated methods listed
> in the inherited API below — build on it: call it, extend it via `super()`, or override
> it deliberately. Do not reimplement inherited behavior.

No config knob; it is prompt prose that is essentially never wrong. Its text is part of
the build fingerprint like the rest of the contract block (one-time restale of
whole-class modules on upgrade — function-only projects are untouched; see §5).

### Inherited generated API block

For each **spec'd** base that has a built artifact on disk, the builder renders the
generated class's public API — signatures + docstrings — from the artifact via
`module_api.build_generated_class_api_summary` (`module_api.py:268-296`), **replacing**
the runtime-object MRO snapshot for that base. External / non-spec bases keep the
existing `resolve_base_contract` runtime walk (it is correct for them: they don't change
mid-build). Because the scheduler orders A before B (§2) and payloads are assembled when
a component runs (`_component_payload`, `builder.py:1654-1727`), B always reads fresh-A.

Codex path unchanged in shape: the block travels in
`_context/whole_class_contract.md` (`codex_backend.py:341-345`) and
`base_contract_block` (`codex_backend.py:419`).

## 4. Validation

`validate_build_class_source` (`validation.py:799-895`) changes:

- **Sealed drift is an error.** Each sealed method's generated signature must match the
  spec's AST-normalized signature exactly: parameter names, order, kinds, defaults,
  annotations, and return annotation (normalization reuses the digest layer's
  canonicalization so formatting/quoting differences don't false-positive). Errors feed
  `generate_with_retry` (`generate/base.py:117-185`) like any validation failure.
- **Guidepost drift stays warn-only** (`class_build_warnings`,
  `validation.py:903-929`), and the dropped-parameter warning is kept as-is.
- Everything else is unchanged: stubs implemented and non-stub, preserved methods
  AST-equivalent, declared bases and class decorators and attributes preserved,
  abstractmethods implemented, docstring retained.

A sealed method also passes through the existing "must exist / must not remain a stub"
checks by virtue of being a stub member.

## 5. Freshness, staleness, digest compatibility

- **Tier participates in the contract digest** — but only when non-default. Sealed
  membership is appended to `_normalized_members` (`digest.py:304-343`) **only for
  sealed methods** (e.g. a `"sealed"` tag on that member's tuple). Marker-free projects
  produce byte-identical digests → no mass rebuild on upgrade. Adding/removing an inner
  `@jaunt.magic` changes the class digest → rebuild, as it should.
- **Base API staleness.** B's `module_context_digest` (`builder.py:726-746`)
  incorporates, for each spec'd base, the base's `generated_public_api_digest`
  (`module_api.py:268-296`) read from the artifact on disk — replacing the current
  frozen-runtime `base_contract_block` hash for spec'd bases. Interface change in
  generated A ⇒ B's context digest moves ⇒ B stale. Body-only rebuild of A (same public
  API) ⇒ digest unchanged ⇒ B untouched. Unbuilt base ⇒ a sentinel value that flips once
  A first builds.
- **Prompt-template fingerprint.** The contract-block text changes (§3), which restales
  existing *whole-class* modules once. Acceptable and honest: their prompts genuinely
  changed. Function-only modules see no change.
- The Layer B semantic gate is unaffected: sealed-signature edits are structural
  (Layer A catches them); gate behavior for docstring-only edits is unchanged.

## 6. Testing

Unit (all against the mocked backend; no API keys):

- `test_magic_decorator.py` / new: inner `@jaunt.magic` absorbed (registry has one
  whole-class spec, zero method specs; `sealed_members` populated); bare and called
  forms; standalone method-magic on an undecorated class unchanged.
- Errors: magic+preserve stacked; inner magic on non-stub body; inner magic with kwargs.
- `test_class_analysis.py`: 3-way `MemberSplit`; `is_magic_decorator` both forms;
  scaffold strips inner magic; mode classification counts sealed as stubs.
- `test_prompt_quality.py` / contract render: three tier sections present; composition
  paragraph present; inherited generated API block rendered from a disk artifact.
- `test_deps.py`: `class B(A)` both `@magic`, **inference off** ⇒ edge exists, topo
  order A before B; base cycle ⇒ cycle error.
- `test_validation_class.py`: sealed signature drift (renamed param, changed default,
  changed return annotation) ⇒ error; guidepost drift ⇒ warn only.
- `digest`: marker-free class digest byte-identical to pre-change; adding inner magic
  changes it; B restaled by A's public-API change; B *not* restaled by A's body-only
  change.
- End-to-end (`test_builder_whole_class.py`): `class B(A)` both `@magic` through a full
  mocked build — ordering, inherited-API injection, validation. Runtime: generated B
  calling `super().method()` into generated A works through import-time substitution.

## 7. Documentation

- `CLAUDE.md` whole-class bullet: describe the three tiers and always-on base edges.
- `jaunt instructions` primer: tier vocabulary + one sealed example.
- Docs site: `writing-specs/magic.mdx` (tiers table), `writing-specs/dependencies.mdx`
  (base edges are structural, not inferred), `reference/change-detection.mdx` (base API
  digest propagation).
- The bundled jaunt skill (`.claude/skills/`) if it documents whole-class magic.

## Decision log

- **Three-tier vocabulary** over "keep everything soft" or "strict by default" — makes
  the existing softness an explicit contract and adds a lock where users need one.
- **Always-on structural base edge** over inference-gated or explicit `deps=` — the
  class header already states the dependency.
- **Composition guidance always-on** over per-class or config toggles — prompt prose
  with no realistic off-case.
- **Scope: whole-class `@magic` only** — method-level `@magic` and contract mode
  unchanged.
- **Inner `@jaunt.magic` as the sealed marker** over a new `@jaunt.seal` /
  `@jaunt.sig` decorator or a `sealed=[...]` kwarg — reuses the exact existing meaning
  ("Jaunt implements this, as declared"), keeps the marker on the method it governs,
  and adds no new public symbol. Rejected `@jaunt.preserve` reuse: preserve's defining
  purpose is marking stub-*looking* bodies as hand-written; overloading it on body shape
  would invert that guarantee.
