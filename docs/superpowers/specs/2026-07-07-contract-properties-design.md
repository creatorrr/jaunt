# Contract mode `properties` case kind (Hypothesis-backed) — Design

**Date:** 2026-07-07
**Status:** Sketch for review (fast-follow deferred from contract-mode v1; see
`2026-06-23-contract-mode-design.md` §Non-goals)
**Topic:** Add `"properties"` to `[contract] derive` — property/invariant cases in
derived batteries, rendered as vanilla Hypothesis code, with the model deriving the
*oracle* and Hypothesis deriving the *inputs*.

## Prior art: `hypothesis-auto` (and why we don't depend on it)

[`timothycrosley/hypothesis-auto`](https://github.com/timothycrosley/hypothesis-auto)
is the closest existing tool: point `auto_pytest_magic(fn)` at a type-annotated
function and get pytest property cases with zero authored test code. Reviewed
2026-07-07:

- **Not adoptable as a dependency.** Last release (1.1.5) is from ~2020 with
  fossilized pins: `pydantic >=0.32.2, <2.0.0` (v1, EOL) and
  `hypothesis >=4.36, <6.0.0` (Hypothesis is on 6.x). It cannot coexist with a
  modern stack.
- **Its core trick is now native Hypothesis.** Strategy inference from type
  annotations is `st.from_type()` / `st.builds()`, and the `hypothesis write`
  ghostwriter subsumes the codegen angle.
- **Its weakness is the L5 trap** (`docs/principles/2026-06-29-building-with-coding-agents.md`,
  "Properties over examples for the contract core"). Annotations give you
  *generators*, not *oracles*: out of the box `hypothesis-auto` asserts only
  "doesn't crash, or raises an allowed exception"; the real contract check is the
  optional hand-written `auto_verify_` callback. Properties without a real
  invariant are decorative randomness.

**What we take from it:** the zero-authoring ergonomics (a derived battery region,
not user-written `@given` boilerplate) and the `auto_verify_` shape — except Jaunt
derives that callback from the docstring contract instead of asking the user for
it. **What we do differently:** generate plain `hypothesis` code directly into the
committed battery; no runtime library between the battery and Hypothesis.

## Position in the contract pipeline

Nothing about the contract-mode architecture changes. A property case is one more
derived region in the committed battery:

- **Authoring**: a `Properties:` docstring section, parsed by the same
  section-grammar `Examples:`/`Raises:` use (`contract/cases.py`,
  `_case_lines_for_section`).
- **Derivation**: structured bullets parse deterministically (no model); prose
  bullets go through `reconcile`'s model derivation, same trust posture as
  unstructured examples — the model output is committed, diffed, and
  human-reviewed before it gates anything.
- **Artifact**: a `# >>> jaunt:derived properties` region in the battery
  (`contract/battery.py` markers), ordinary pytest + Hypothesis, runnable with no
  Jaunt and no API key.
- **Gate**: `jaunt check` runs it like any other battery test. Determinism is
  preserved by construction (§Determinism).
- **Drift**: no new states. The `Properties:` text is part of the cleaned
  docstring, so editing it is prose drift (stale → block → `reconcile`), and a
  property failure at check time is behavior drift, exactly like a failing
  example.

## Authoring surface — the `Properties:` section

Two bullet tiers, mirroring how `Examples:` already splits into a deterministic
call-equality form and a model-transcribed prose fallback:

### Tier 1 — structured bullets (deterministic, no model)

```
Properties:
- given t: str :: slugify(slugify(t)) == slugify(t)
- given a: str, b: str :: slugify(a + " " + b) == slugify(a + "-" + b)
- given xs: st.lists(st.integers()) :: sorted(dedupe(xs)) == sorted(set(xs))
```

Grammar: `given <bindings> :: <boolean-expr>`.

- `<bindings>` is a comma-separated list of `name: <type-or-strategy>`. A plain
  type expression maps to `st.from_type(<type>)`; an expression rooted in `st.`
  is passed through verbatim as an explicit strategy override (the escape hatch
  `hypothesis-auto` spelled `auto_parameters_`).
- `::` separates bindings from the invariant expression. Chosen because it is
  unambiguous against both the annotation colons in bindings and the `->` arrow
  the legacy example form uses.
- `<boolean-expr>` must reference the target (checked with the existing
  `_call_root_and_method` rooting walk); free names are classified by the
  existing `_classify_names` (fixture / module import / builtin), so property
  cases get `Fixtures:` support and battery `extra_imports` for free.

Parsing lands in `contract/cases.py` beside the `Examples:`/`Raises:` logic: a new
`PropertyCase` (bindings + expression, plus the shared `fixtures`/`imports`/
`is_async` fields) and a `properties: tuple[PropertyCase, ...]` slot on
`CaseBlocks`. Note `cases.py` is a **self-hosted magic module** — this change is
an edit to the `parse_case_blocks` docstring contract plus
`jaunt build --target jaunt.contract.cases`, not a hand-written body.

### Tier 2 — prose bullets (model-derived at `reconcile`)

```
Properties:
- Output is idempotent: slugifying a slug returns it unchanged.
- Output never contains consecutive hyphens or uppercase letters.
```

A bullet that doesn't parse as Tier 1 is prose. At `reconcile` (the only
model-calling command, unchanged) the derivation prompt is extended: the strict
JSON shape in `prompts/contract_derive_system.md` gains a `"properties"` key —
`[{"bindings": "t: str", "expr": "<boolean-expr>"}]` — with the same
transcribe-don't-invent rules. The model's job is converting a stated invariant
into a checkable expression, **not** inventing invariants the prose doesn't
state: a docstring with no `Properties:` section derives no property cases (see
§Explicitly rejected for the fully-automatic alternative).

This is the highest-model-trust case kind so far (examples are author-written
rows the model transcribes; a property expression is model-*formulated*). Three
things bound it, all already in the architecture:

1. The derived region is committed and reviewed like any test diff before it
   gates anything (contract-mode risk posture, unchanged).
2. `reconcile` runs the new property against the real body before freezing; a
   property that fails at derive time is surfaced as a finding ("prose and body
   already disagree"), never silently committed.
3. Tier 1 exists, so an author who wants zero model formulation writes the
   expression themselves and still gets generated Hypothesis plumbing.

## Rendered battery region

```python
# >>> jaunt:derived properties
from hypothesis import given, settings, strategies as st

@given(t=st.from_type(str))
@settings(max_examples=50, derandomize=True, deadline=None)
def test_prop_1(t):  # derived from: Properties
    assert slugify(slugify(t)) == slugify(t)
# <<< jaunt:derived properties
```

- Rendering is a new `_render_properties_cases` in `contract/derive.py`, wired
  into `derive_case_regions` under `if "properties" in derive` — the same shape
  as the examples/errors renderers, including `region_suffix` for per-method
  class regions. `derive.py` stays **handwritten by choice** for the same reason
  it is today: rendered battery bytes feed the deterministic `check` gate.
- The `hypothesis` import lives *inside* the derived region, not the battery
  preamble, so batteries without property cases are byte-identical to today's
  output and `parse_battery`'s preamble stripping is untouched.
- Async targets follow the existing pattern: `@pytest.mark.asyncio` +
  `async def` + awaited call, driven by the already-shipped pytest-asyncio.

## Determinism (`check` stays a pure function of the code)

Every rendered property gets `@settings(derandomize=True, deadline=None)`:

- `derandomize=True` is Hypothesis's CI mode — example generation becomes
  deterministic per (test, strategy) with no database or seed file, so battery
  pass/fail depends only on committed code. No `.hypothesis/` directory
  materializes in adopter repos, and `check` keeps its "deterministic, offline"
  guarantee.
- `deadline=None` because Hypothesis's per-example deadline is wall-clock-based
  and a top flakiness source in CI; the battery's pytest run already has
  process-level timeouts.
- The coverage trade (derandomize explores less over time than a persisted
  database) is paid deliberately. `reconcile` compensates: after a property
  first derives (or its expression changes), reconcile runs one exploratory
  **non**-derandomized pass with a larger budget before freezing, so flaky or
  wrong properties are shaken out at the moment a human is already reviewing,
  not later in CI. Failures surface as derive-time findings.

`max_examples` defaults to 50 (matching `hypothesis-auto`'s default),
configurable via `[contract] property_max_examples`.

## Dependency

`hypothesis>=6` joins the base install (`pyproject.toml`), consistent with the
batteries-included policy that already ships pytest/pytest-asyncio/anyio.
Batteries remain runnable without Jaunt — a property battery needs `hypothesis`
the way today's batteries need `pytest`, and adopters running `jaunt check` have
Jaunt (hence hypothesis) in the environment. `reconcile` refuses to derive the
`properties` kind with an actionable error if `hypothesis` is not importable in
the project environment (relevant only to adopters who strip deps).

## Interaction with strength scoring

Mutation scoring (`contract/strength.py`) runs pure cases in-process per mutant
via `_evaluate_mutant_killed`. Running 50 Hypothesis examples per mutant is the
wrong cost profile, so v1 of this feature:

- **Excludes property cases from the mutation loop**, exactly as fixture-backed
  cases are excluded today, and counts them in the existing
  `strength_excluded` header field so the score stays honest about what it
  measured.
- Leaves the door open (config `[contract] strength_properties = false`) for a
  budgeted mode later: re-run each property at `max_examples=5, derandomize=True`
  inside the existing per-mutant timeout. Properties are strong mutant-killers,
  so this is attractive — but it should follow measurement, not precede it.

The inverse relationship matters more: **properties raise effective strength
indirectly** by catching real drift `check`-time, which is where the L5
"tests pass but behavior is wrong" gap actually closes.

## Explicitly rejected: annotation-only auto-fuzz as a default

The fully-automatic `hypothesis-auto` mode — derive a "doesn't crash on valid
inputs (modulo documented `Raises:`)" case from annotations alone, no docstring
needed — was considered as a third tier. Rejected as a *default* because it is
precisely the decorative-randomness failure mode L5 warns about, and it would
dilute what a passing battery means. Kept as a possible explicit opt-in
(`derive = [..., "fuzz"]`, a separate kind so `"properties"` never implies it):
model-free, nearly free to emit, occasionally useful as a smoke tier for adopted
legacy code with rich annotations and thin docstrings. Not part of this
feature's v1.

## Configuration

```toml
[contract]
derive = ["examples", "errors", "properties"]  # opt-in; default stays ["examples", "errors"]
property_max_examples = 50                     # per-property Hypothesis budget in the battery
strength_properties = false                    # (future) budgeted property runs in mutation scoring
```

`_VALID_DERIVE` in `config.py` grows `"properties"` (strict-config rejection of
typos comes for free). Default `derive` is unchanged — existing projects see
zero behavior or byte diff until they opt in.

## Deliverables (file-by-file)

- `config.py` — `"properties"` in `_VALID_DERIVE`; `property_max_examples`
  (+ reserved `strength_properties`) on the `[contract]` section.
- `contract/cases.py` *(self-hosted magic — edit the docstring contract, rebuild)*
  — `Properties:` section parsing, `PropertyCase`, `CaseBlocks.properties`,
  Tier-1 `given … :: …` grammar with rooting + name classification.
- `contract/derive.py` *(handwritten by choice)* — `PropertyRow` in the model
  payload shape, `_render_properties_cases`, `derive_case_regions` wiring,
  region-local `hypothesis` import.
- `contract/runner.py` — pass property cases through derive/merge; exploratory
  non-derandomized pass at reconcile; hypothesis-importable precondition.
- `contract/strength.py` *(self-hosted magic)* — exclude property cases from the
  mutant loop; fold the exclusion into `strength_excluded`.
- `prompts/contract_derive_system.md` / `contract_derive_user.md` — extend the
  strict-JSON shape with `"properties"`; transcribe-don't-invent rules for
  invariants.
- `pyproject.toml` — add `hypothesis>=6` to base dependencies.
- Tests — Tier-1 grammar (parse/root/classify/reject), renderer bytes
  (derandomize + deadline always present; no-property batteries byte-identical
  to today), mocked-model Tier-2 derivation, merge preserves hand-added cases
  around the new region, strength exclusion accounting, config validation.
- Example — extend a contract example with one Tier-1 and one Tier-2 property
  (idempotence is the canonical demo for `slugify`).
- Docs — `CLAUDE.md` `[contract]` block, contract-mode docs, the `jaunt` skill.

## Open questions

1. **Tier-1 separator spelling.** `given … :: …` is proposed; alternatives were
   `forall`/`->`. Confirm before the grammar lands in the `cases.py` contract.
2. **`from_type` failure mode.** `st.from_type` raises at collection time for
   unresolvable annotations (e.g. bare protocols). Proposed: reconcile-time
   validation resolves every binding's strategy once and surfaces a finding,
   so the committed battery never fails at collection.
3. **Test-function naming.** Positional (`test_prop_1`) vs. a slug of the
   expression. Positional is stable under expression edits only if ordering is
   stable; a content slug churns names on any edit. Leaning positional-in-
   source-order (matches how parametrize rows behave today).
4. **Class contracts.** Property bullets in method docstrings should compose
   with `region_suffix` like examples do, but instance construction inside a
   `given` needs a story (probably: Tier 1 requires the expression to construct
   or receive the instance explicitly; no implicit `st.builds(Cls)` in v1).
