# Contract mode `properties` case kind (Hypothesis-backed) — Design

**Date:** 2026-07-07
**Status:** First cut implemented (this PR). Revised for the PR-review findings:
`database=None` in rendered settings, rooting walks the whole invariant, binding
names are import-classified, prose+structured bullets merge, and fixtures/async
targets are rejected in v1 rather than claimed "for free". (Originally a sketch
for the fast-follow deferred from contract-mode v1; see
`2026-06-23-contract-mode-design.md` §Non-goals.)
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
- The invariant is usually an `ast.Compare`/`ast.BoolOp`, not a bare call, so
  rooting **walks the whole expression** and requires at least one call rooted
  in the target (each rooted call is checked individually via the existing
  `_call_root_and_method` helper).
- Free names from **both** the invariant and the binding type/strategy
  expressions are classified against the module's top-level names for battery
  `extra_imports` (a binding like `p: Path` needs `Path` importable at
  collection time even though it never appears in the invariant). Unknown
  names fail loudly, mirroring the example grammar.
- **Fixtures are rejected in v1** with an actionable error: pytest fixtures are
  function-scoped by default and Hypothesis reuses them across every generated
  example (`HealthCheck.function_scoped_fixture`), so "free" fixture support
  would be unsound. **Async targets are rejected in v1** too — `await` cannot
  appear in an `eval`-mode invariant expression.

Parsing lives in a **new handwritten module `contract/properties.py`** (not in
`cases.py` as originally sketched): `PropertyCase`/`PropertyBlocks`, the Tier-1
parser, and the Hypothesis renderer. It reuses the handwritten grammar helpers
from `cases.py` (`_case_lines_for_section`, `_call_root_and_method`,
`_names_in`, …) so the two grammars cannot drift, while leaving the self-hosted
magic spec in `cases.py` (`parse_case_blocks`) untouched — adding properties
never restales it, and no `codex` rebuild is needed to ship the feature. A
welcome side effect: property cases are excluded from the mutation-strength
loop **by construction** (they are not in `CaseBlocks`, which is all
`compute_case_strength` sees).

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

**Structured and prose bullets merge** (review finding): when a `Properties:`
section mixes both, reconcile parses the Tier-1 bullets deterministically and
sends **only the leftover prose bullets** through the model — not gated on the
overall block being empty. Model rows are rendered back into Tier-1 `given … ::
…` bullets and re-parsed by the same deterministic grammar, so a malformed row
fails loudly at reconcile instead of producing an unparseable battery. Tier-2
is function-path only in v1; class/method docstrings get Tier-1 only.

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
from hypothesis import given, settings
from hypothesis import strategies as st

@given(t=st.from_type(str))
@settings(max_examples=50, derandomize=True, database=None, deadline=None)
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

Every rendered property gets `@settings(derandomize=True, database=None,
deadline=None)`:

- `derandomize=True` makes example generation deterministic per (test,
  strategy); `database=None` is required **in addition** because Hypothesis
  keeps derandomization and the example database independent (its CI profile
  sets both) — without it, a local run could replay or save examples under
  `.hypothesis/examples` and pass/fail would not be a pure function of
  committed code. `check` keeps its "deterministic, offline" guarantee.
- One residue remains even with both set: Hypothesis writes **derived caches**
  (unicode tables, a constants pool scanned from the code under test) under
  `.hypothesis/` in the cwd. These are caches of derived data, not replayed
  examples — outcomes are unaffected — but a CI gate should not dirty the
  tree, so jaunt-driven battery runs (`check`/`reconcile`/`status`) set
  `HYPOTHESIS_STORAGE_DIRECTORY` to `.jaunt/hypothesis` (respecting a
  user-set value). A **direct** `pytest` run of a property battery may still
  create `.hypothesis/` locally; adopters should gitignore it. (Found in the
  live shim-backed run, not on paper.)
- `deadline=None` because Hypothesis's per-example deadline is wall-clock-based
  and a top flakiness source in CI; the battery's pytest run already has
  process-level timeouts.
- The coverage trade (derandomize explores less over time than a persisted
  database) is paid deliberately. `reconcile` compensates: after a property
  first derives (or its expression changes), reconcile runs one exploratory
  **non**-derandomized pass with a larger budget before freezing, so flaky or
  wrong properties are shaken out at the moment a human is already reviewing,
  not later in CI. Failures surface as derive-time findings. *(First cut: the
  exploratory pass is deferred; reconcile validates the derandomized battery
  via pytest before writing, which catches wrong-but-deterministic properties.
  The exploratory pass remains the designed follow-up.)*

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

- **Excludes property cases from the mutation loop** — by construction, since
  they live in `PropertyBlocks` rather than the `CaseBlocks` that
  `compute_case_strength` consumes — and counts them in the existing
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

## Deliverables (file-by-file, as implemented)

- `config.py` *(contract mode — body-only change, benign drift)* —
  `"properties"` in `_VALID_DERIVE`; `property_max_examples` on `[contract]`
  (`strength_properties` reserved for the budgeted follow-up, not yet a key).
- **New** `contract/properties.py` *(handwritten)* — `PropertyBinding` /
  `PropertyCase` / `PropertyBlocks`, the Tier-1 `given … :: …` parser
  (bindings via a dict-literal parse; invariant-wide rooting walk; import
  classification over invariant + bindings; fixture/async rejection), the
  Hypothesis region renderer (`derandomize=True, database=None,
  deadline=None`; region-local hypothesis imports; positional
  `test_prop_<i>` names; `properties-<method>` region suffixes), and
  `properties_extra_imports`.
- `contract/derive.py` *(handwritten by choice)* — `PropertyRow` +
  `ContractBlocks.properties` in the model payload shape.
- `contract/runner.py` — property parsing on both the function and class
  paths; prose-bullet model merge (function path); Tier-1 round-trip of model
  rows; hypothesis-importable precondition; pytest validation whenever
  property cases exist; `strength_excluded += len(property cases)`;
  extra-imports union; `model_extract` now `(prose, func_name)` so the model
  can emit target-rooted expressions.
- `cli.py` — thread `property_max_examples` through `reconcile`/`adopt`; pass
  the real function name into the model closure.
- `prompts/contract_derive_system.md` — strict-JSON shape gains
  `"properties"`; transcribe-don't-invent rules for invariants.
- `pyproject.toml` — `hypothesis>=6` in base dependencies.
- Tests (`tests/test_contract_properties.py`) — Tier-1 grammar
  (parse/root/classify/reject incl. the review findings), renderer bytes,
  no-property batteries byte-identical across derive sets, mocked-model Tier-2
  merge (model sees only prose bullets + real func name), failing property
  blocks the write, strength-excluded accounting, class-method suffix regions,
  config validation.
- Deferred from the first cut: the exploratory non-derandomized reconcile
  pass, a runnable example project, and `jaunt` skill / contract-docs updates
  beyond `CLAUDE.md`.
- `contract/cases.py` and `contract/strength.py` *(self-hosted magic)* —
  deliberately **untouched**; see §Authoring surface for why the grammar lives
  in its own module.

## Open questions — resolved in the first cut

1. **Tier-1 separator spelling.** Kept `given … :: …`.
2. **`from_type` failure mode.** Covered by reconcile's pytest validation: any
   battery containing property cases is run before it is written, so a
   collection-time `from_type` failure surfaces as a reconcile finding and the
   broken battery is never committed. (A friendlier targeted message than the
   generic validation failure is possible follow-up polish.)
3. **Test-function naming.** Positional in source order (`test_prop_1`, …),
   matching how parametrize rows behave today.
4. **Class contracts.** Method-docstring bullets compose with
   `region_suffix` (`properties-<method>`, `test_prop_<method>_<i>`); the
   expression must construct or receive the instance explicitly
   (`Counter(n).peek() == n`) — no implicit `st.builds(Cls)` in v1.
