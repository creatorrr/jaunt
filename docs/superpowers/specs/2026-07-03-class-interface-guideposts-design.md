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
`runtime.py:267-268`) and returns `_make_method_wrapper` (`runtime.py:363-403`). When
the class-level `@jaunt.magic` then runs (`runtime.py:270-297`), it:

1. **queries the registry** for method specs whose `module` matches and whose
   `class_name` equals this class's name — absorption keys off the registry, not
   wrapper-sniffing (the wrappers carry no jaunt marker today). This requires a new
   internal registry operation, `unregister_magic(spec_ref)` (`registry.py` has no
   removal API).
2. **unregisters** those method specs and records the member names as the whole-class
   spec's `sealed_members`.
3. **restores the original functions** onto the class body it registers. The wrapper is
   built with `functools.wraps(fn)`, so the original is recoverable via `__wrapped__`.
   Descriptor stacking must be reconstructed: a member slot holding
   `classmethod(wrapper)` / `staticmethod(wrapper)` (correct decorator order puts the
   descriptor outermost) is unwrapped via `__func__`, the original recovered, and the
   same descriptor re-applied. `@abstractmethod` flags are carried over. This keeps the
   registered runtime object and the source AST in agreement.

Descriptor support matrix for sealed members (v1): plain methods, `@classmethod`,
`@staticmethod`, and `@abstractmethod` stacking are supported; **`@property` + inner
`@jaunt.magic` is rejected** with a clear error (`_unwrap_from_class`,
`runtime.py:348-360`, does not handle property descriptors even for standalone method
magic — sealing properties waits until that path exists).

If the class is never decorated, the inner specs remain standalone method specs —
existing behavior, zero migration. Because absorption happens at import time, discovery
(`discovery.py`) and `jaunt specs` never see phantom method specs for absorbed members.

**The mixed-magic guard changes meaning.** `_build_expected_names`
(`builder.py:1203-1238`) currently errors when a class has both whole-class and
per-method `@magic` ("Use one or the other."), and `tests/test_builder_methods.py`
locks that behavior. After absorption, that registry state is unreachable via a real
import — mixing *is* the sealed feature. The guard stays as defense-in-depth for
hand-constructed registry states, but its message is reworded to point at absorption
("inner @magic methods of a whole-class spec should have been absorbed; this indicates
a registration bug") and the locking test is updated accordingly.

On the build side, detection is AST-based and does not depend on the runtime absorption:
`class_analysis.py` already has a private `_is_magic_decorator` (`class_analysis.py:144`)
— it is promoted/reused for member classification, and `MemberSplit` grows a third
bucket: `MemberSplit(stubs, sealed, preserved, preserve_marked)`. `classify_class_mode`
counts sealed methods as stubs (a class of only sealed methods is still `stubs` mode).
The seed scaffold (`build_class_scaffold`, `class_analysis.py:207-247`) strips inner
`@jaunt.magic` exactly as it strips `@jaunt.preserve`.

### Errors (decoration-time where possible, discovery-time otherwise)

- `@jaunt.magic` + `@jaunt.preserve` stacked on one method → error (contradictory tiers).
- Inner `@jaunt.magic` on a **non-stub** body inside a whole-class spec → error:
  "hand-written body: use `@jaunt.preserve` to keep it, or reduce it to a stub for Jaunt
  to implement."
- Inner `@jaunt.magic(...)` with any kwargs inside a whole-class spec → error (v1).
- `@property` combined with inner `@jaunt.magic` inside a whole-class spec → error (v1,
  see support matrix above).
- The existing wrong-order guard (`@classmethod`/`@staticmethod` above `@magic`,
  `runtime.py:199-207`) applies to inner magic unchanged.

Note on timing: at method-decoration time the runtime cannot know the class will be
whole-class-decorated, so the kwargs / non-stub / property errors fire at
**class-decoration (absorption) time**; the stacked-markers error can fire earlier via
AST analysis at discovery. All surface as config/discovery errors (exit 2), before any
model call.

## 2. Dependency graph: always-on base edges

`SpecEntry` (`registry.py:25-39`, frozen/slotted) gains two fields:
`sealed_members: tuple[str, ...] = ()` (recorded during absorption, §1) and
`base_deps: tuple[SpecRef, ...] = ()` — normalized at decoration time from the project
base refs `resolve_base_contract` already computes (`class_analysis.py:102-138`) —
recorded **separately** instead of merged into `auto_deps` (today's
`runtime.py:236-247` merge is removed). `resolve_base_contract` records any non-stdlib
base, so `build_spec_graph` filters `base_deps` against known specs before adding edges
(a hand-written project base is context, not a dependency node).

`build_spec_graph` (`deps.py:226-379`) applies the filtered `base_deps`
**unconditionally**, at the same rank as explicit `deps=` — not gated by `infer_deps`
overrides or `infer_default` (`deps.py:252-274`). Rationale: inheritance is structural
fact from the class header, not an inference; B cannot be planned without A.
Consequences:

- **Cross-module base** (A and B in different modules): A's module always builds before
  B's; A's generated public API lands in B's build context (the Codex path seeds
  dependency API `.pyi` files, `codex_backend.py:332`) even with inference off, and
  `graph_digest` transitivity (`digest.py:354-384`) covers the base.
- **Same-module base** (A and B in one source module): `collapse_to_module_dag`
  (`deps.py:382-400`) drops same-module edges by design, and connected specs generate as
  one component — there is **no** "A artifact on disk before B". That is the correct
  behavior, not a gap: the model designs A and B coherently in a single shot, seeing
  both specs, and staleness is trivial because they rebuild together. The
  inherited-generated-API mechanics (§3) and base-API staleness (§5) therefore apply to
  **cross-module spec'd bases only**; same-module inheritance is handled by
  co-generation plus the in-prompt base contract.
- A cross-module base cycle is a hard cycle error (exit 2) — correct: it is a real cycle.

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

No config knob; it is prompt prose that is essentially never wrong. Note that today the
whole-class contract block is **not** hashed anywhere — `module_context_digest`
(`builder.py:726`) does not include it, and the generation fingerprint
(`generate/fingerprint.py:69`) hashes prompt *template files* only, not this rendered
block. §5 makes the contract block participate in freshness explicitly; the one-time
restale of whole-class modules on upgrade comes from that change (function-only projects
are untouched).

### Inherited generated API block

For each **cross-module spec'd** base that has a built artifact on disk, the builder
renders the generated class's public API — signatures + docstrings — from the artifact
via `module_api.build_generated_class_api_summary` (`module_api.py:268-296`),
**replacing** the runtime-object MRO snapshot for that base. External / non-spec bases
keep the existing `resolve_base_contract` runtime walk (it is correct for them: they
don't change mid-build); same-module spec'd bases are co-generated and need no artifact
view (§2). Because the scheduler orders A's module before B's (§2) and payloads are
assembled when a component runs (`_component_payload`, `builder.py:1654-1727`), B
always reads fresh-A.

Codex path unchanged in shape: the block travels in
`_context/whole_class_contract.md` (`codex_backend.py:341-345`) and
`base_contract_block` (`codex_backend.py:419`).

## 4. Validation

`validate_build_class_source` (`validation.py:799-895`) changes:

- **Sealed drift is an error.** Each sealed method's generated signature must match the
  spec's AST-normalized signature exactly: parameter names, order, kinds, defaults,
  annotations, and return annotation. This needs a real data path: today
  `_class_validation_inputs` (`builder.py:582-630`) passes only `stub_methods` and
  `preserved_segments`, and the digest layer's signature rendering is a plain
  `ast.unparse` string (`digest.py:258`), not an equality API. Add a
  `sealed_signatures` field to the validation inputs and a **canonical signature
  comparator** (AST-normalized, formatting/quoting-insensitive) shared by digest and
  validation so the two layers cannot disagree. Errors feed `generate_with_retry`
  (`generate/base.py:117-185`) like any validation failure.
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
- **Base API staleness (cross-module spec'd bases).** B's `module_context_digest`
  (`builder.py:726-746`) incorporates, per such base, a hash of the **rendered inherited
  generated API block** (§3) — signatures *and* docstrings, read from A's artifact on
  disk. Hashing the rendered block rather than `generated_public_api_digest` is
  deliberate: that digest hashes only member names/signatures (`module_api.py:292`),
  but docstrings are behavioral contract in jaunt and are injected as B's context, so a
  doc change in A's public API must restale B. Body-only rebuild of A (same signatures,
  same docstrings) ⇒ block unchanged ⇒ B untouched. Unbuilt base ⇒ a fixed
  `unbuilt:<ref>` sentinel.
- **One computation, one timing rule.** Context digests are computed in three places
  today — `jaunt build`/`jaunt status` precompute before scheduling (`cli.py:2236`,
  `status_core.py:158`), `run_build` recomputes before writing headers
  (`builder.py:1805`, `1932`), and refreeze reuses precomputed header fields
  (`builder.py:460`). The base-API contribution is centralized in one helper used by
  all three, with this rule: **headers always store the post-dependency (fresh) digest**
  — `run_build` computes B's contribution after A's module completed, so the stored
  header matches what a subsequent `status` recomputes from the same artifacts (no
  flapping). The `unbuilt` sentinel appears in a header only when A genuinely failed to
  build; A's later success then legitimately restales B once.
- **Refreeze guard.** The Layer B refreeze path rewrites header digests over an
  unchanged generated body using precomputed values. When B's stored context digest
  differs from the fresh one *because a spec'd base's API block moved* (or is the
  `unbuilt` sentinel), refreeze is not allowed — fail safe to a full rebuild, consistent
  with the existing "any gate doubt ⇒ rebuild" policy.
- **Whole-class contract block joins the context digest.** The rendered contract block
  (tier sections + composition paragraph, §3) is hashed into `module_context_digest`
  for whole-class modules — it is real prompt input and today isn't fingerprinted at
  all (§3). This is what restales existing whole-class modules once on upgrade.
  Function-only modules see no change.
- The Layer B semantic gate is unaffected: sealed-signature edits are structural
  (Layer A catches them); gate behavior for docstring-only edits is unchanged.

## 6. Testing

Unit (all against the mocked backend; no API keys):

- `test_magic_decorator.py` / new: inner `@jaunt.magic` absorbed (registry has one
  whole-class spec, zero method specs; `sealed_members` populated); bare and called
  forms; standalone method-magic on an undecorated class unchanged; originals restored
  with `@classmethod`/`@staticmethod`/`@abstractmethod` descriptors reconstructed.
- Errors: magic+preserve stacked; inner magic on non-stub body; inner magic with
  kwargs; `@property` + inner magic rejected.
- `test_builder_methods.py`: the existing mixed-magic conflict test (~line 260) is
  reworked — a real import never reaches the guard anymore; the guard's
  defense-in-depth message is asserted on a hand-constructed registry state instead.
- `test_class_analysis.py`: 3-way `MemberSplit`; `is_magic_decorator` both forms;
  scaffold strips inner magic; mode classification counts sealed as stubs.
- `test_prompt_quality.py` / contract render: three tier sections present; composition
  paragraph present; inherited generated API block rendered from a disk artifact.
- `test_deps.py`: `class B(A)` both `@magic`, **inference off** ⇒ edge exists, topo
  order A before B; base cycle ⇒ cycle error.
- `test_validation_class.py`: sealed signature drift (renamed param, changed default,
  changed return annotation) ⇒ error; guidepost drift ⇒ warn only.
- `digest`: marker-free class digest byte-identical to pre-change; adding inner magic
  changes it; B restaled by A's public-API signature change *and* by a docstring-only
  change to A's public API; B *not* restaled by A's body-only change; header digest
  equals a post-build `jaunt status` recomputation (no flap); refreeze refused when the
  base API block moved (falls back to rebuild).
- End-to-end (`test_builder_whole_class.py`): cross-module `class B(A)` both `@magic`
  through a full mocked build — ordering, inherited-API injection, validation;
  same-module `class B(A)` co-generated as one component with no conflict error.
  Runtime: generated B calling `super().method()` into generated A works through
  import-time substitution.

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
- **Same-module inheritance = co-generation** (post-review): `collapse_to_module_dag`
  drops same-module edges by design, so artifact-based inherited context and base-API
  staleness apply to cross-module bases only; a same-module base pair is designed
  coherently in one component and rebuilds together.
- **Hash the rendered inherited-API block, not `generated_public_api_digest`** (post-
  review): the existing digest ignores docstrings, but docstrings are contract and are
  injected as subclass context — doc changes to a base's public API must restale
  subclasses.
