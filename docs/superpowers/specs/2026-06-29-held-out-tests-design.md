# Held-out tests (implementer/tester independence in the generation harness) — Design

**Date:** 2026-06-29
**Status:** DRAFT — design approved in brainstorming; pending plan + implementation.
**Engine assumption:** Codex is the sole engine (`codex exec`), per
`docs/superpowers/specs/2026-06-24-codex-engine-design.md`.
**Principle anchor:** This is the jaunt-side concretization of **L14 / §1.19**
("treat tests as a held-out set; keep the implementer and tester blind to each
other") and **§2.3 roadmap item 9** in
`docs/principles/2026-06-29-building-with-coding-agents.md`.

> **Design philosophy (user-set):** *prompts and roles do the heavy lifting;
> mechanism only where it is irreducible.* The barrier is carried by two clearly
> briefed roles (Implementer, Tester). The single piece of irreducible mechanism is
> the **repair-feedback redactor** — a prompt cannot un-leak an assertion diff that
> is already sitting in the text handed to the model — plus the **fail-safe-to-held-
> out** default that guards it.

> **Codex review (2026-06-29, `gpt-5.5@high`, read-only against source):** verdict
> *basically sound directionally.* Four fixes were folded in before planning — the
> role briefings must live in `CodexBackend._build_prompt`, not the `prompts/*.md`
> templates (§4); the per-item structured report must carry full detail so the redactor
> never falls back to global pytest stdout (§5/§6); the fail-safe needs an explicit
> collection/import-phase policy (§6); and `public_api_only` is a *partial* guard, not a
> hard white-box block (§8). See the inline "(Codex review)" notes and §10.

---

## 1. Context & motivation

Jaunt already has a **de-facto two-agent split**: `jaunt build` runs the
implementation generator (the *Implementer*, build prompt) and `jaunt test` runs the
test generator (the *Tester*, test prompt) as separate `codex exec` invocations with
separate contexts. The tester-side of a held-out barrier is, in fact, already
enforced:

- The test prompt context **never receives generated implementation source**:
  `dependency_generated_modules={}` for the test path (`tester.py:1144`); the Tester
  gets only public dependency signatures + the module contract.
- `public_api_only` defaults to `True` (`module_contract.py:138`, applied at
  `tester.py:1099`/`1108`), and the validator (`_validate_public_api_only_test`,
  `validation.py:608-683`) rejects tests that import the generated module, inspect
  `__globals__`/private attributes, or monkeypatch the target — with a deliberate
  `@jaunt.test(public_api_only=False)` white-box opt-out.

Two gaps remain — neither is a missing barrier so much as a missing *framing* and one
real leak:

1. **Neither role is told it is in a held-out regime.** The test prompt says "public
   API only by default" (`prompts/test_module.md:42`) but never states the barrier,
   its asymmetry, or its rationale. So neither side calibrates its *planning* to the
   regime (the Implementer does not know to generalize rather than pattern-match; the
   Tester does not know its suite is the sole gate and should be adversarial).
2. **The repair path leaks held-out expected values.** When `jaunt test`'s generated
   tests fail pytest, jaunt regenerates the implicated implementation
   (`tester.py:1456-1466`) and feeds the Implementer a **compacted dump of raw pytest
   stdout/stderr** (`_compact_failure_context`, `tester.py:951`, 28-line truncation),
   injected as `"Previous attempt problems:\n…"` (`codex_backend.py:427-428`). Under
   the default `-q`, that output contains assertion diffs — `assert 41 == 42` — so the
   **held-out expected value flows straight into the Implementer's regeneration
   prompt.** It is bounded (single repair pass — not loop-to-green; truncated; never
   includes test source or the contract battery), so it is *not* adaptive-holdout
   territory yet. But it is the exact L14 violation, and it becomes serious the moment
   repair iterates.

This design closes both: it **briefs the two roles** and **redacts the repair
feedback** along a public/held-out tier line.

## 2. Goals / Non-goals

**Goals**
- The Implementer is **never handed a held-out expected value.** Repair feedback for a
  *derived* (held-out) test failure is redacted to `{opaque-case-id,
  exception-class}`; feedback for an *example* (public) test failure keeps full
  detail. (Fixes the live leak in §1.2.)
- Both roles are **explicitly briefed** with the barrier *and its rationale*: an
  Implementer prompt section and a Tester prompt section, so each calibrates planning,
  not just complies.
- The Tester **derives expected values from the contract, never from observed
  behavior** (the test-oracle discipline), stated in-prompt; existing
  `public_api_only` enforcement is retained.
- **Examples are the shared/public tier; everything the Tester derives is held-out.**
  Provenance is **model-declared** (the Tester tags each case), with a **fail-safe**:
  untagged/ambiguous → treated as held-out (redacted), never leaked.

**Non-goals**
- **No hard enforcement** of implementer-side blindness. It stays *incidental but
  explicit* (build runs before test generation, so the Implementer is blind at gen
  time) — stated in prose + an invariant comment, not enforced with machinery.
- **No deterministic example extractor.** Provenance is model-declared; deterministic
  extraction of docstring examples is noted as a *future hardening* (§12), not v1.
- **No per-role model de-correlation.** Running the Tester on a different
  model/effort than the Implementer (so they do not share blind spots) is **parked**
  (§12) — a `jaunt.toml` knob for later, orthogonal to this design.
- **No multi-round repair loop.** Repair stays single-pass; this design does not add
  iteration (and would require an explicit query budget if it ever did — §12).
- Not changing module-level generation granularity; not making `jaunt check` /
  `jaunt status` call a model.

## 3. Design overview

```
                       shared oracle: the docstring contract
                      ┌──────────────────────┬──────────────────────┐
                      ▼                       ▼                      ▼
              IMPLEMENTER (build)                            TESTER (test)
              briefed: held-out regime,                     briefed: held-out author,
              redacted derived failures,                    suite is the sole gate,
              don't pattern-match                           derive oracle from contract
                      │                                              │
                      │ generates impl                               │ generates tests,
                      │ (blind to tests:                             │ tags each case:
                      │  build precedes test-gen)                    │  jaunt_tier=example | derived
                      ▼                                              ▼
              __generated__/<mod>.py                        __generated__/test_<mod>.py
                      └───────────────────┬──────────────────────────┘
                                          ▼
                                run pytest (jaunt tier plugin):
                                per-test {nodeid, tier, outcome, exc-class}
                                          │ any failure → repair
                                          ▼
                                TIERED REDACTOR (replaces raw stdout dump)
                                  example failure → full detail (assert diff, tb)
                                  derived failure → {opaque-id, exception-class}
                                  untagged/ambiguous → treated as derived (fail-safe)
                                          │
                                          ▼
                                Implementer regen prompt (single repair pass)
```

## 4. The two roles + policy prompts (component 1 — the centerpiece)

The barrier is primarily carried here. Two prompt sections are added, gated by
`ctx.kind`. **Important (Codex review):** the Codex backend assembles the `codex exec`
prompt **inline** in `CodexBackend._build_prompt` (`codex_backend.py:391`) from
`ctx.*` blocks and does **not** render the `prompts/*.md` templates (the same gap as
principle roadmap item 1 — those templates aren't even in the Codex fingerprint). So
editing `prompts/*.md` alone would be dead under the Codex engine. The held-out
sections must be injected **into `_build_prompt` directly**, branched on `ctx.kind`:
the Implementer section on the build/implementation pass, the Tester section when
`ctx.kind == "test"`. (If the `prompts/*.md` templates are kept in sync for non-Codex
documentation, that's fine — but `_build_prompt` is the load-bearing path.)

**Implementer — "held-out regime" section (build prompt):**
- A separate Tester writes the acceptance tests. **You will never see them.**
- On a failed build–test cycle you receive feedback for *derived* cases as
  `{case-id, exception-class}` only — **no expected values, by design.**
- *Rationale (stated, not just imposed):* if you could read the tests you would be
  tempted to satisfy the specific cases instead of the contract; the barrier exists so
  your implementation generalizes. Treat it as a closed-book exam graded by an
  independent examiner.
- *Directive:* do not try to infer, probe, or pattern-match to the hidden cases. When
  the example checks pass but derived checks fail, **re-read the contract for the
  general rule** rather than special-casing the reported failure.

**Tester — "held-out author" section (test prompt):**
- The Implementer sees only redacted pass/fail of your derived cases. **Your suite is
  the real gate** — make derived cases adversarial, not mirrors of an obvious
  implementation.
- **Derive every expected value from the contract** (docstring / signature / declared
  examples), **never** from reasoning about how the code probably behaves. Commit the
  expected value before considering behavior. (This is the oracle discipline,
  component 3, folded in here.)
- **Tag every generated test** with `@pytest.mark.jaunt_tier("example")` if it asserts
  a behavior given as a *canonical example* in the docstring, or
  `@pytest.mark.jaunt_tier("derived")` for any case you derived yourself. **Name
  derived cases opaquely** (e.g. `test_derived_01`); descriptive names like
  `test_empty_list_returns_zero` are themselves answer hints.

These sections are the bulk of the work and the bulk of the behavior change. Mechanism
(§5–§6) exists only to make the Implementer prompt's "no expected values" promise true.

## 5. Tiering & tagging (component 4 — model-declared, fail-safe)

- **Marker:** `@pytest.mark.jaunt_tier("example" | "derived")` on each generated test
  function, emitted by the Tester per §4. A small **jaunt pytest plugin** registers
  the marker (no "unknown mark" warnings) and records, **per test item and per phase
  (setup/call/teardown)**, `{nodeid, tier, outcome, exception_class, longrepr,
  captured_stdout, captured_stderr, warnings}`. Capturing the full detail **up front,
  per item** is what lets the redactor (§6) build `example` feedback without ever
  touching global pytest stdout (Codex review — otherwise derived leaks re-enter
  through the shared output).
- **Default (the one mechanical safety we insist on):** a test with **no**
  `jaunt_tier` marker, or an unrecognized value, is classified **`derived`**
  (held-out). So a model mistag or omission fails toward **over-redaction** (the
  Implementer loses a little debug detail), never toward leak (the answer key
  escapes). This keeps the barrier off the model's good behavior for the one property
  that matters.
- **Structured output, fully replacing stdout scraping:** the plugin writes a
  structured per-item report (JSON) that the repair-context builder consumes, **fully
  replacing** the `_compact_failure_context` stdout/stderr scrape (`tester.py:951`) — the
  redactor must never fall back to global pytest stdout, because that text mixes tiers
  (warning summaries, captured stdout/stderr, collection text, parametrized-id strings,
  global sections) and would re-introduce derived leaks. `longrepr`/captured-output/
  warnings are surfaced **only for `example`-tier** items; for `derived` the redactor
  keeps nothing but the opaque id + exception class.

## 6. Redaction in the repair path (component 5 — the irreducible mechanism)

Replace the raw-stdout error context fed to the Implementer on repair
(`tester.py:951` → `initial_error_context_by_module` → `codex_backend.py:427-428`)
with a **tiered redactor** built from the §5 structured report:

- **`example`-tier failure** → full detail: assertion diff + traceback (the
  Implementer's legitimate public debug surface — examples are part of the shared
  contract).
- **`derived`-tier failure** → redacted line: `{opaque-id, exception-class}` only. No
  expected/actual, no failing input, no traceback, no descriptive test name. The
  plugin assigns each derived nodeid a **stable opaque id** (`derived#<n>`), so the
  name never reaches the Implementer.
- **Collection / import / pre-item failures (Codex review — the main residual leak
  edge):** a syntax error, import error, or module-level side effect that fails *before*
  test items exist has **no `Item` and therefore no `jaunt_tier` to read** — and its
  traceback can contain generated **test source**. Policy: treat any such failure as
  `derived` (redact to `{exception-class, failing-module}`, no traceback) **unless it
  can be proven to originate only in example-tier code**. `xfail`/`skip` are fine *only
  if their reasons are never surfaced* to the Implementer; a `strict` xpass follows the
  same tier rules as a failure. This is the fail-safe extended to the phases where no
  marker exists.
- **Budget:** repair stays **single-pass** (it already is — `tester.py` runs one
  repair cycle, not loop-to-green; per-generation attempts remain `2 + ty_attempts`,
  `builder.py:1499`). No new iteration is introduced. *If* a future change makes repair
  iterate, held-out-informed retries must get an explicit cap (Dwork adaptive-holdout
  bound) — see §12.
- **Auditability:** every derived failure surfaced to the Implementer is logged
  (nodeid → opaque-id, exception-class), so a reviewer can reconstruct what signal
  crossed the barrier.

Note this also *narrows* what the Implementer sees relative to today even for the
non-leak parts (truncated stdout becomes a clean per-case list), which is a usability
win, not just a security one.

## 7. Implementer-side blindness — explicit, incidental, not enforced (component 2)

Per the user's steer (enforcement is more work than it is worth here):

- **Keep the incidental property:** `jaunt test` builds before it generates tests, so
  the concrete assertions do not exist when the implementation is written; and the
  build prompt's attached-test info is gated by `include_target_tests` (**default
  `False`**, `cli.py:1722-1728`) and is stub-only when on (signature + docstring
  intent via `extract_source_segment`, `builder.py:928-935` / `digest.py:19-46`) —
  never concrete assertions.
- **Make it explicit:** state the property in the Implementer prompt (§4) and add a
  one-line invariant comment at the build-context assembly site and the repair call
  site, so a future refactor sees the intent ("the Implementer must not receive
  generated test source or held-out expected values").
- **Optional cheap guard (not full enforcement):** an assertion that the assembled
  build context contains no `__generated__` test-module source. One check, no new
  subsystem.

## 8. Oracle discipline + contract-mode alignment (components 3 & 7)

- **Component 3 (oracle discipline):** carried by the Tester prompt section in §4
  ("derive from the contract, precommit before considering behavior"). No heavy new
  lint: the existing `public_api_only` validator blocks the most common white-box
  reaches **inside the expected test functions**. *Caveat (Codex review):* it validates
  only those expected functions (`validation.py:551`), and base validation allows extra
  top-level helpers (`validation.py:570`) — so a helper that reaches into generated-module
  internals and is called by a clean-looking test slips through. We accept this as a
  *partial* guard under the "minimal mechanism" steer; extending validation to helpers
  reachable from public tests (or validating the whole generated test module) is listed
  as optional hardening (§12). The characterization-snapshot risk is a *prompt* concern,
  addressed in §4.
- **Component 7 (contract-mode alignment):** the same two role sections apply to both
  the magic-mode test path and contract mode. Contract mode already embodies the
  oracle discipline — its deriver produces falsifiable checks *from the docstring* and
  **never returns a verdict**, scored by mutation strength — so alignment is mostly
  making magic-mode's test prompt say out loud what contract mode already does. The
  `jaunt_tier` example/derived split maps cleanly onto contract mode's existing
  `derive = ["examples", "errors"]` (examples → `example` tier; errors and any further
  derivation → `derived`).

## 9. Configuration & flags (minimal)

- **No new required config.** The white-box opt-out (`@jaunt.test(public_api_only=
  False)`) already exists for specs that legitimately need it.
- **Debug escape hatch:** `--no-redact-derived` on `test` (and the repair path) emits
  full pytest detail for all tiers — for local debugging only, off by default, and
  logged loudly when used (it intentionally defeats the barrier).
- **Parked:** the de-correlation knob (per-role model/effort) is *not* added here
  (§12).

## 10. Grounding & review trail (what the code does; what reshaped the design)

Two read-only code investigations (Explore) and one Codex consult
(`gpt-5.5@xhigh`, web-research) established the facts and reshaped the design:

- **Tester-side already isolated** (Explore): `dependency_generated_modules={}` for
  tests (`tester.py:1144`); `public_api_only` default + validator guards
  (`validation.py:608-683`). So this design does **not** rebuild the tester-side; it
  briefs it.
- **The leak is in repair, and it is the live finding** (Explore): pytest stdout
  (incl. assertion diffs) is fed back to the Implementer on repair
  (`tester.py:951`/`1456-1466`, `codex_backend.py:427-428`). This moved component 5
  from "future guardrail" to "small live correctness fix."
- **Build-time implementer-side is already clean** (Explore): `include_target_tests`
  defaults `False`; attached test info is stub-only when on. So component 2 needs only
  to be made *explicit*, not enforced.
- **Codex reshaping:** "coarse pass/fail is not enough under *adaptive* reuse" (Dwork
  reusable-holdout) — but jaunt's repair is single-pass, so we are safe today and only
  need a cap *if* repair ever iterates (§12). "Test names leak" → opaque ids for
  derived cases (§6). "Tester anchoring / characterization" → derive-from-contract,
  precommit (§4/§8). The asymmetry (a test's output can be the answer key, an impl's
  output cannot) is the reason the redaction is one-sided.
- **User decisions:** prompts/roles do the heavy lifting; component 2 explicit-not-
  enforced; provenance model-declared **with** the fail-safe-to-held-out default
  (§5); de-correlation parked.
- **Codex design review (2026-06-29, `gpt-5.5@high`, read-only):** verdict "basically
  sound directionally"; four fixes folded in — (1) role sections belong in
  `CodexBackend._build_prompt`, not `prompts/*.md` (§4); (2) the per-item structured
  report carries full detail so the redactor never touches global stdout (§5/§6); (3) an
  explicit collection/import-phase fail-safe (§6); (4) `public_api_only` is a *partial*
  guard, softened + listed as optional hardening (§8/§12). Grounding checks (repair
  feeds raw pytest stdout, `include_target_tests` default False,
  `dependency_generated_modules={}`, single repair cycle) all confirmed accurate.

## 11. Testing

Unit (mocked backend, no API key — matches the existing suite):
- **Redactor:** a `derived`-tier failure yields exactly `{opaque-id,
  exception-class}` with no expected/actual/input/name; an `example`-tier failure
  yields full detail. An **untagged** failure is treated as `derived` (fail-safe).
- **Opaque id stability:** the same derived nodeid maps to the same opaque id across a
  run; the descriptive name never appears in the redacted feedback.
- **Plugin classification:** marked tests are recorded with their tier; unmarked →
  `derived`; unrecognized tier value → `derived`.
- **Repair wiring:** on a mocked pytest failure, the regeneration prompt
  (`codex_backend` error context) contains the redacted feedback, **not** raw stdout;
  assert the string `assert ` / `== ` expected-value pattern is absent for derived
  failures and present for example failures.
- **Single-pass budget:** repair invokes generation once per failure (no loop).
- **Prompt content:** the build prompt contains the held-out-regime section; the test
  prompt contains the held-out-author + tagging + oracle-discipline sections.
- **`--no-redact-derived`:** full detail for all tiers, and a loud log line.
- **Blindness invariant:** the assembled build context contains no `__generated__`
  test-module source (the §7 guard).

Integration: a spec whose example checks pass but whose derived checks fail is
repaired with only redacted derived feedback (mocked), and the regeneration prompt is
asserted free of expected values.

## 12. Open questions / risks / parked items

- **Over-redaction → repair thrash.** Redacting derived failures to `{id, exc-class}`
  may give the Implementer too little to fix genuine ambiguity, raising rebuild churn.
  Mitigation is the public example tier (full detail) + the "re-read the contract"
  directive. Watch repair success rate after rollout; if it drops, consider widening
  derived feedback to include the *exception message* (still no expected value) — a
  middle tier.
- **Model-declared provenance is only as honest as the Tester.** A mistagged
  `example` (that is really adversarial) would leak its detail. The fail-safe defaults
  *untagged* → derived, but cannot catch a positively-mistagged case. **Future
  hardening (parked):** a deterministic example extractor (doctest `>>>` / a structured
  `Examples:` block) that classifies provenance structurally instead of trusting the
  marker — this is the stronger L4/L14 form and dovetails with contract mode's
  `examples` derivation.
- **Helper-function white-box gap (optional hardening, Codex review).**
  `public_api_only` validates only the expected test functions, not helper functions
  they call (`validation.py:551`/`570`), so a helper reaching generated-module internals
  can slip past the guard. Extending validation to helpers reachable from public tests —
  or validating the whole generated test module — would close it; deferred under the
  "minimal mechanism" steer.
- **De-correlation (parked).** Implementer and Tester share one engine/model today, so
  they share blind spots — the independence is partial. A `[test]`/`[codex]` knob to
  run test generation on a different model/effort/framing is a cheap future addition,
  intentionally out of scope here.
- **Iterating repair (future).** If repair is ever made to loop, derived-informed
  retries must get an explicit query budget (adaptive-holdout bound); the single-pass
  property is what keeps the current redaction sufficient.
- **Repair routing can mutate the held-out oracle (Codex review of the implementation).**
  jaunt infers which modules a failure implicates from the pytest output *paths*; a plain
  derived assertion failure (`assert add_one(1) == 42`) usually names only the generated
  *test* file, so `implicated_build_modules` stays empty and the **test** is regenerated
  (`tester.py:1495`/`1545`) rather than the implementation — i.e. the held-out oracle can
  rewrite itself to fit the code (an orchestration-level tautology). This is *pre-existing*
  routing, untouched by this work (which only redacts feedback *content*), but the held-out
  principle puts it in tension. Follow-up: on a derived-tier failure, bias toward
  regenerating the *implementation*, and never silently regenerate the very held-out test
  that failed — while still allowing a genuinely buggy test to be fixed deliberately. Out
  of scope for this PR.
- **Plugin delivery.** Confirm during planning how the jaunt pytest plugin is made
  available to the generated test run (entry-point plugin vs. injected `conftest.py`)
  and that `jaunt clean` removes any structured-report artifact it writes.
