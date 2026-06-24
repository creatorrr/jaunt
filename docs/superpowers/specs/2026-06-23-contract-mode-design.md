# Contract mode (inverted source of truth) — Design

**Date:** 2026-06-23
**Status:** Draft for review (design locked via brainstorming; pre-implementation)
**Topic:** A second authoring mode where committed code is the source of truth, the
docstring is the contract, and Jaunt maintains a derived, committed test battery
instead of generating the implementation.

## Problem & motivation

Jaunt today is **Magic mode**: the docstring is canonical, the function body is a
stub (`raise RuntimeError("spec stub …")`), the real implementation is generated
into a parallel `__generated__/` tree, and at import time the `@magic` wrapper
swaps the stub for the generated symbol (`runtime.py:262-271` for functions,
`runtime.py:215-242` for classes, `_make_method_wrapper` at `runtime.py:291` for
methods). English is the source of truth; code is a disposable build artifact.

That bet has three costs that show up on a medium project rather than a demo:

- **Reproducibility is coupled to a vendor's model lifecycle.** The bytes you ship
  depend on what the provider's model produced. Pinning helps until the model is
  retired — the eval table in `README.md` records an `anthropic:opus-4.6` `404
  not_found` and a `cerebras 402 payment_required` in the same run set. A build
  whose output can 404 is a build dependency on a third party's billing and
  deprecation schedule.
- **You author twice.** For real logic the docstring approaches the size of the
  code it replaces (`examples/01_slugify`: 101 spec lines → 98 generated lines),
  once in a checkable language (Python) and once in an uncheckable one (English).
- **The output is un-editable by contract.** Generated files carry "DO NOT EDIT";
  to change behavior you edit English and re-roll, even when a three-character code
  fix is obvious.

**Contract mode inverts the relationship.** The committed function — real body,
real file — is the source of truth and the thing that runs. The docstring is the
*contract*. Instead of generating the implementation, Jaunt derives a **committed
test battery** from the contract prose and keeps body and contract in agreement.
The model becomes a dev-time tool you run on purpose (`jaunt reconcile`), never a
build dependency: `jaunt check` and CI run only committed tests, with no API key.

This does not replace Magic mode. Per the migration decision, the two modes
**coexist indefinitely** as first-class, decorator-keyed pipelines.

## Goals

- Code is canonical and committed; `@jaunt.contract` is a runtime no-op marker.
- The contract (docstring prose) drives a **derived, committed, human-reviewed**
  test battery that is ordinary pytest — runnable and readable without Jaunt.
- `jaunt check` is deterministic, offline, and needs no API key; the model runs
  only at `jaunt reconcile`.
- Drift between body and contract is **detected deterministically** and surfaced,
  never silently auto-resolved.
- A **strength score** (mutation-based) tells the user, per function, whether a
  docstring is a real contract or decoration.
- Frictionless on-ramp (`adopt`) and off-ramp (`eject`): adopting needs no rewrite,
  ejecting leaves plain Python plus plain pytest with no Jaunt residue.
- Magic mode is untouched and remains fully supported.

## Non-goals (v1)

These are designed here but **deferred to fast-follow**, not built in v1:

- **Property / invariant derivation** (Hypothesis-backed). v1 derives example and
  error cases only.
- **Stability score** and **round-trip ambiguity detection** (re-derive prose from
  unchanged input, compare).
- **Ensemble derivation** (N independent derivations, low-agreement cases flagged).
- **Reverse direction `jaunt doc`** (regenerate prose from the body) and
  `reconcile --regen-body` (closed-loop regenerate the body to satisfy the battery).
- **Whole-class / per-method / async contracts.** v1 covers **top-level sync
  functions**. (Class and method contracts interact with the whole-class Magic
  design; sequenced after.)
- **Soft-eject** (drop only the prose, keep signature+test tracking).
- A `[check] stale = "warn"` knob — v1 blocks on stale, full stop.

Also out of scope: replacing or deprecating Magic mode; custom metaclasses.

## 1. Authoring model — `@jaunt.contract`

A single `@jaunt.contract` marker on a normal, fully-implemented function. The body
is real, committed, and runs as written. The docstring is the contract.

```python
@jaunt.contract
def slugify(title: str) -> str:
    """
    Convert a human title into a URL-safe slug.

    Examples:
    - "  Hello, World!  " -> "hello-world"
    - "C++ > Java" -> "c-java"

    Raises:
    - ValueError if the title is empty after cleaning.
    """
    cleaned = _NON_ALNUM_RUN_RE.sub("-", title.strip().lower()).strip("-")
    if not cleaned:
        raise ValueError("title is empty after cleaning")
    return cleaned
```

- **Runtime is a no-op.** `@jaunt.contract` returns the function unchanged, like a
  type annotation. There is no import-time substitution, no `__generated__` import,
  no `JauntNotBuiltError` path. The function you commit is the function that runs.
- **Registration only.** At import the decorator registers a `SpecEntry`
  (`kind="contract"`) in the registry (`registry.py`) so discovery can find it,
  exactly as `@magic`/`@test` register today (`runtime.py:194-209`). It does not
  install a wrapper.
- **The contract is the cleaned docstring**, the same text Jaunt already treats as
  the behavioral contract in Magic mode.

## 2. Relationship to Magic mode (coexistence)

Both modes are first-class and selected **by decorator**, not by config:

| | Magic mode (`@jaunt.magic`) | Contract mode (`@jaunt.contract`) |
|---|---|---|
| Source of truth | docstring (English) | committed code |
| Body in spec file | stub (`raise RuntimeError`) | real implementation |
| Model produces | the implementation → `__generated__/` | a test battery → test tree |
| Runtime | import-time substitution | no-op marker |
| Generate/refresh | `jaunt build` | `jaunt reconcile` |
| CI/verify | run generated code + `jaunt test` | `jaunt check` (no model) |
| Tests | `@jaunt.test` stubs → generated | derived from the contract prose |

- A project may use **both** in different modules. Discovery (`discovery.py`) scans
  for all three decorators; the registry tags each entry's `kind`.
- Shared infrastructure: the LLM/runtime backend (`[llm]`, `[agent]`/`[aider]`,
  `generate/base.py`'s `GeneratorBackend`), the dependency graph (`deps.py`), and
  the digest/header machinery (`digest.py`, `header.py`).
- Distinct pipelines: `build`/`test` operate on `@magic`/`@test`;
  `reconcile`/`check`/`adopt`/`eject` operate on `@contract`. `status` reports both.
- Contract mode does **not** use `@jaunt.test`. Its tests are derived from the
  contract docstring, not from separate test stubs.

## 3. The contract — prose → derived battery

The model **never returns a verdict** ("does this body satisfy the contract?").
It **derives falsifiable checks** from the prose, which then run deterministically
against the committed body. "Drift" becomes "derived case #7 failed on input X" —
reproducible and locatable.

**v1 derives two case kinds, both low model-trust:**

- **Example cases** — lifted from an `Examples:` block (input → expected output).
  These are *author-written ground truth*; the model's job is extraction and
  formatting into parametrized assertions, not invention.
- **Error cases** — from a `Raises:` block (input → exception type), likewise
  author-stated.

This is why v1 starts here: the strongest derived cases are human-authored rows the
model only transcribes, so the battery is trustworthy without solving "is the
model's interpretation correct." Model-invented **property** cases (where that
question bites) are a fast-follow (§Non-goals).

**Prose stays free; structure is an optional hint, not a DSL.** The deriver keys
off conventional section headers it already sees in Jaunt docstrings (`Examples:`,
`Raises:`, and later `Properties:`). Prose without those headers still gets derived,
just less richly — which shows up as a **lower strength score** (§7) rather than an
error. Structure is rewarded, never required.

## 4. The committed battery (the artifact)

The derived battery is an **ordinary pytest module** written into the test tree,
reviewable and runnable by anyone with no Jaunt installed and no API key. This is
deliberately the most boring, portable artifact in Python — and unlike Magic mode's
`__generated__/` tree it neither runs in production nor resists hand-editing,
because tests are *supposed* to be a separate, reviewable artifact.

Path: `<test_root>/contract/<spec_module_path>.py` (default `tests/contract/…`),
configurable via `[contract] battery_dir`.

```python
# This file is maintained by jaunt (contract mode). Review like any test.
# jaunt:contract
# jaunt:derived-from=slugify_demo.specs:slugify
# jaunt:prose-digest=sha256:91a3…
# jaunt:signature=sha256:0c12…
# jaunt:body-digest=sha256:7f55…
# jaunt:strength=7/8
# jaunt:tool_version=0.4.x
import pytest
from slugify_demo import slugify

@pytest.mark.parametrize("raw,want", [
    ("  Hello, World!  ", "hello-world"),
    ("C++ > Java", "c-java"),
])
def test_examples(raw, want):              # derived from: Examples
    assert slugify(raw) == want

@pytest.mark.parametrize("raw", ["", "   ", "---"])
def test_empty_after_cleaning_raises(raw):  # derived from: Raises
    with pytest.raises(ValueError):
        slugify(raw)
```

The header reuses and extends `header.py` (`format_header`/`parse_header`). New keys:
`contract` (mode marker), `derived-from` (source spec ref), `prose-digest`,
`signature`, `body-digest`, `strength`. The body below the header is hand-reviewable
pytest and may be lightly hand-edited; reconcile preserves human-added cases (it
appends/updates derived cases under their `# derived from:` markers and leaves the
rest, similar in spirit to the preserve/heuristic split in the whole-class design).

## 5. Drift model & digests (deterministic, no model)

`check` and `status` compute a per-function state with **no model call**, from four
cheap inputs: `current_prose_digest`, `current_signature`, `current_body_digest`
(all hashed from the live source) versus the battery header's recorded values, plus
the battery's pass/fail.

States, in precedence order:

1. **Unbuilt** — `@jaunt.contract` present, no battery on disk. → **block**: "no
   contract battery; run `jaunt reconcile`."
2. **Stale (prose drift)** — `current_prose_digest != header.prose-digest`. The
   contract was edited without re-deriving. → **block** (decision C, lockfile
   semantics): "contract changed; run `jaunt reconcile`."
3. **Signature drift** — `current_signature != header.signature`. → **block**.
4. **Behavior drift** — battery run has any failure. The body no longer satisfies
   the contract's pinned cases. → **block**: surface failing cases; fix the body or
   `reconcile`.
5. **Refactored (benign)** — `current_body_digest != header.body-digest` but prose
   and signature match and the battery passes. → **pass**, with an informational
   note in `status` ("body changed since last reconcile; contract still satisfied").
   This is the core affordance: hand-edit the body freely as long as the committed
   tests stay green.
6. **In sync** — all digests match, battery passes. → **pass**.

Steps 1–3 are pure hashing and short-circuit before the battery runs; step 4 is
plain pytest. Nothing here needs the model, an API key, or the network.

## 6. Commands

Only `reconcile` calls the model. `check`/`status` are deterministic; `adopt` and
`eject` are local edits (adopt derives once, via reconcile).

### 6.1 `jaunt adopt <path|ref>`
On-ramp for existing code, **no rewrite**. Add the `@jaunt.contract` marker to a
function that already has a docstring, derive its battery (via reconcile), and if
the current body passes, commit the battery and report the strength score. If the
body *fails* its own derived contract, surface that — the docstring and code already
disagree, which is a useful finding, not an error.

### 6.2 `jaunt reconcile [--target ...]` (the only model-calling command)
For each function that is new, prose-changed, or `--force`d:
1. Derive the battery from the contract prose (examples + errors, §3).
2. Run it against the committed body.
   - **Passes** → write/refresh the battery and header (prose/signature/body
     digests), compute strength (§7), report "in sync."
   - **Fails** → surface the failing derived cases and **stop** (v1 does not auto-
     edit code). The user decides: fix the body, or refine the prose / drop an
     over-derived case, then re-run. (`reconcile --regen-body`, the closed-loop
     body regeneration, is a fast-follow.)
Reconcile preserves hand-added test cases and only manages the `# derived from:`
regions.

### 6.3 `jaunt check` (CI gate, no model)
Runs the §5 state machine across all `@contract` functions and exits non-zero on any
**block** state. This is the pre-commit hook and CI gate. Deterministic, offline,
no API key. Equivalent to "run the committed batteries + verify nothing is stale."

### 6.4 `jaunt status`
Extends the existing `status` (`cli.py`) to report contract functions alongside
Magic staleness: each function's state (§5), its strength score, and **DAG fallout**
— when a contract's prose changed, downstream dependents are flagged `review` (§8).
`--json` supported, matching existing convention.

### 6.5 `jaunt eject <ref> [--all]`
Off-ramp, the inverse of `adopt` and a **metadata operation** (no code moves):
- Remove the `@jaunt.contract` marker from the function.
- Strip the `jaunt:` header keys from the battery, leaving a one-line provenance
  comment; the file becomes a plain, hand-owned test module.
- Drop the entry from the registry so `reconcile`/`check` no longer consider it.
Nothing changes at runtime (the marker was a no-op). The function becomes plain
Python with a docstring; its tests become plain pytest. **Leaving costs nothing.**
Eject is the durable, honest resolution to repeated drift (versus silently re-
blessing): "the tests are the contract here; stop deriving from prose." It is also
the **cost lever** — track the churning functions, eject the stable majority, whose
committed tests still guard them in CI for free. Ejecting a function with a low
strength score **warns** first (you are freezing weak tests; strengthen the contract
or accept it).

> Reverse direction `jaunt doc` (regenerate prose from the body, then re-derive and
> confirm the new prose still accepts the body) is designed but **fast-follow**.

## 7. Contract strength (mutation scoring) — v1 trust hook

Prose-as-contract has a silent failure: a docstring that reads like a spec but pins
nothing. The strength score measures whether the contract is real by **mutating the
body and re-running the battery**: a contract that survives a broken body is
decoration, not a contract.

- **Scoped, homegrown AST mutator** over the single function body (not whole-suite
  mutation — that is slow and module-granular). Operator set (v1): comparison swaps
  (`<`↔`<=`, `==`↔`!=`, …), boolean connective swap (`and`↔`or`), condition negation,
  boundary mutation on integer constants (`n`→`n±1`), arithmetic swaps, constant
  replacement (`True`↔`False`, `0`↔`1`, `s`→`""`), statement deletion, and
  return-value defaulting (`return x`→`return None`).
- For each mutant: rebind the mutated function in isolation and run the committed
  battery; the mutant is **killed** if any test fails. Mutants that fail to compile
  or exceed a per-mutant timeout are skipped. `strength = killed / applicable`,
  stored as `strength=K/N` in the header.
- Runs at `reconcile` on the accepted battery+body. Surfaced by `status`, and used
  as the `eject` readiness gate.

**Honest limitation (circularity).** The mutator is pure AST and model-independent,
so it objectively perturbs the body. The residual risk is a *shared blind spot*: if
the model mis-derived a case the same way a human mis-wrote the body, mutation can't
catch it. Two things bound this in v1: the strongest cases are **author-written
`Examples:` rows** (not model-invented, §3), and the score is **advisory** — it
informs and gates `eject`, it does not block `check`.

## 8. Dependency graph & cascade

- Contract mode reuses the existing dependency graph: explicit `deps=` on
  `@jaunt.contract` plus AST inference (`deps.py`), same as Magic mode.
- **Body change** → re-run only this function's battery (cheap, no model).
- **Prose/contract change** → mark this battery stale (§5.2) **and** flag downstream
  dependents `review` in `status`, because their derived cases may have assumed the
  old behavior. v1 **flags only** — it does not auto re-derive dependents. The graph
  says *where* to look; the user decides *when* to pay for re-derivation.
- An **ejected** function does not leave the DAG; it becomes a **test-pinned node** —
  downstream functions treat its committed tests and signature as the contract
  surface instead of its prose. (This is exactly "tests as the contract," locally,
  for that node.)

## 9. Configuration (`jaunt.toml`)

```toml
[contract]
battery_dir = "tests/contract"     # where derived batteries are written
derive = ["examples", "errors"]    # v1 set; "properties" is fast-follow
strength = true                    # run mutation scoring at reconcile
# v1: stale battery always blocks `check` (no knob)
```

Reuses `[llm]` and `[agent]`/`[aider]` for the derivation backend. No new provider
wiring — derivation is a new prompt over the existing `GeneratorBackend`.

## 10. Deliverables (file-by-file)

- `runtime.py` + `jaunt/__init__.py` — add and export `@jaunt.contract` (a no-op
  identity decorator that registers a `kind="contract"` `SpecEntry`). No wrapper, no
  generated-module import.
- `registry.py` — accept and tag `kind="contract"` entries.
- `discovery.py` — discover `@contract` functions alongside `@magic`/`@test`.
- New module `contract/derive.py` — prose → battery derivation (examples + errors),
  a new prompt over `GeneratorBackend`; preserves hand-added cases.
- New module `contract/battery.py` — battery file format (header + `# derived from:`
  regions), read/merge/write; extends `header.py` keys.
- New module `contract/drift.py` — the §5 deterministic state machine over
  prose/signature/body digests + battery result.
- New module `contract/strength.py` — scoped AST mutator + scoring (§7).
- `header.py` — new keys (`contract`, `derived-from`, `prose-digest`, `signature`,
  `body-digest`, `strength`) in `format_header`/`parse_header`.
- `digest.py` — prose/signature/body digest helpers for contract functions.
- `deps.py` — include `@contract` nodes; prose-change → downstream `review` fallout.
- `cli.py` — new `adopt`, `reconcile`, `check`, `eject`; extend `status` (+`--json`).
- `config.py` — `[contract]` section (`battery_dir`, `derive`, `strength`).
- `prompts/contract_derive.md` (+ system) — the derivation prompt.
- `examples/contract_slugify/` — a runnable contract-mode example (committed code +
  committed battery + a deliberately weak-contract function to show a low strength
  score, and an `adopt` walkthrough).
- Docs — `CLAUDE.md`, `README.md`, and the `jaunt` skill: document Contract mode and
  its relationship to Magic mode.

## 11. Testing plan

- **Decorator/runtime:** `@jaunt.contract` is a true no-op (the decorated function
  is identical and runs its own body); it registers a `kind="contract"` entry; it
  does not import `__generated__` or raise `JauntNotBuiltError`.
- **Derivation (mocked backend):** an `Examples:` block becomes parametrized
  equality assertions; a `Raises:` block becomes `pytest.raises`; hand-added cases
  survive a re-derive; missing structured blocks still derive a (smaller) battery.
- **Drift state machine (no model):** each of the six §5 states is produced by the
  right input combination; stale-prose, signature-drift, and unbuilt **block**;
  refactored body with passing battery **passes** with a note; in-sync passes.
- **check/CI:** `check` exits non-zero on every block state and zero when all in
  sync/refactored; runs with no API key set.
- **Strength:** a strong contract kills most mutants (high score); a vacuous
  docstring with a trivial battery survives mutation (low score); the score lands in
  the header; `eject` warns below a threshold.
- **adopt/eject round-trip:** `adopt` on existing code derives a battery and reports
  strength without rewriting the body; a body that disagrees with its docstring is
  surfaced; `eject` removes the marker, de-jaunts the battery to plain pytest,
  drops registration, and changes nothing at runtime; `adopt` after `eject`
  restores tracking.
- **Cascade:** a prose change on an upstream contract flags downstream dependents
  `review` in `status`; a body-only change does not; an ejected node is treated as
  test-pinned by dependents.
- **Coexistence:** a project mixing `@magic` and `@contract` builds Magic specs and
  reconciles Contract specs independently; `status` reports both.

## 12. Risks & mitigations

- **Model mis-derives a case** → v1 derives only author-written examples/errors
  (low invention); derived batteries are committed and **human-reviewed** like any
  test diff before they gate anything.
- **Strength-score circularity (shared blind spot)** → mutator is model-independent;
  score is advisory, not a `check` gate; strongest cases are author-authored (§7).
- **Stale-block friction** → mitigated by `watch`-style reconcile and a fast,
  function-scoped derive; the lockfile guarantee (contract and tests never silently
  diverge) is judged worth the friction (decision C).
- **`review` cascade noise** → keep it a `status` flag, not a `check` block, in v1;
  `eject` is the honest exit when a node should stop being prose-tracked.
- **Battery drift from hand-edits** → reconcile only manages `# derived from:`
  regions and preserves human-added cases; the body-digest note tells the user when
  a refactor happened.
- **Two modes to learn** → the coexistence table (§2) and decorator-keyed pipelines
  keep the boundary crisp; `adopt`/`eject` make moving between "plain code" and
  "tracked" cheap and reversible.

## 13. Open questions (resolve at spec review)

1. **Naming (provisional throughout):** decorator `@jaunt.contract`; mode name
   "Contract mode"; the derived artifact "battery"/"contract battery"; commands
   `adopt` / `reconcile` / `check` / `eject`. Confirm or rename before implementation.
2. **Mutation engine:** homegrown function-scoped AST mutator (recommended, fast,
   single-function granularity) vs. wrapping `mutmut`/`cosmic-ray` (mature but
   suite-granular and slow). Decision affects `contract/strength.py`.
3. **v1 scope edges:** confirm v1 is **top-level sync functions only** — class,
   per-method, and async contracts deferred (they intersect the whole-class Magic
   design and should be sequenced after it).
4. **`battery_dir` default:** `tests/contract/` under the first test root — confirm
   the layout, especially for projects whose tests are not under a single root.
5. **`reconcile` on failure:** v1 surfaces failing cases and stops (no code edits).
   Confirm `--regen-body` (closed-loop body regeneration) stays a fast-follow.
6. **Lifecycle framing:** plain code → `adopt` → tracked (`reconcile`/`check`) →
   `eject` → plain code. Confirm this is the intended front-door/back-door symmetry.
