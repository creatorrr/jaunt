# Held-out tests — Implementation plan

**Spec:** `docs/superpowers/specs/2026-06-29-held-out-tests-design.md` (read it first).
**Branch:** `feat/held-out-tests` (off `docs/held-out-tests-principle`).
**Execution:** dynamic Workflow of Opus subagents, each driving `codex exec`
(`-m gpt-5.5`, `-c model_reasoning_effort=high`, `-c approval_policy=never`,
`-s workspace-write`) to write the code.

## Hard rules for every agent

- Edit **only** the file(s) assigned to your task. Sibling tasks may have uncommitted
  edits in other files — **never** touch them.
- **Never** run `git checkout`/`reset`/`restore`/`stash`/`clean`/`commit`, and never
  tell codex to revert "out-of-scope" changes. The baseline is committed; leave
  concurrent edits alone.
- Do **not** run the full test suite. Verify only your own file(s): they must parse and
  pass `uv run ruff check <F>`. The dedicated Verify phase runs pytest/ty.
- Drive codex non-interactively; if it leaves a file syntactically broken or missing a
  required symbol, re-run codex with the specific error (max 3 attempts), then report
  what's left. Match surrounding style (`from __future__ import annotations`, line
  length 100, ruff E/F/I/UP/B).

## Frozen interface contracts (so parallel tasks agree)

### `src/jaunt/heldout.py` (Task HELDOUT — new file)
A single cohesive module: the pytest plugin, the structured report, and the redactor.

- Constants: `JAUNT_TIER_MARK = "jaunt_tier"`, `TIER_EXAMPLE = "example"`,
  `TIER_DERIVED = "derived"`, `REPORT_ENV = "JAUNT_HELDOUT_REPORT"`.
- **pytest plugin** (loadable via `-p jaunt.heldout`):
  - `pytest_configure(config)` registers the `jaunt_tier(name)` marker (no "unknown
    mark" warnings).
  - Collect, **per item and per phase (setup/call/teardown)**, a record
    `{nodeid, tier, outcome, exception_class, longrepr, capstdout, capstderr, warnings,
    phase}`. Read `tier` from the item's `jaunt_tier` marker; **default `derived`** if
    absent or unrecognized (the fail-safe).
  - Capture **collection/import errors** (no item ⇒ no marker) as records with
    `tier="derived"` and a `module` field.
  - On `pytest_sessionfinish`, if `REPORT_ENV` is set, write the report JSON to that
    path: `{"items": [...], "collection_errors": [...]}`.
- **Report + redactor** (pure functions, unit-testable without pytest):
  - `load_report(path: str | Path) -> dict` (returns `{}` if missing/corrupt).
  - `assign_opaque_ids(report: dict) -> dict[str, str]` — stable `f"derived#{n}"` per
    derived nodeid, numbered by **sorted** nodeid (deterministic within a run).
  - `build_repair_feedback(report: dict, *, redact: bool = True) -> list[str]` — the
    tiered redactor. For each **failing** record:
    - `example` tier → full detail line(s): nodeid + `longrepr` (+ captured output if
      present).
    - `derived` tier → exactly `f"{opaque_id}: {exception_class}"` — **no** longrepr,
      input, captured output, or nodeid text.
    - collection error → `f"collection error in {module}: {exception_class}"` (derived
      treatment).
    - If `redact=False`: full detail for all tiers (debug escape hatch).
  - **Belt-and-suspenders (component-2 guard):** when `redact=True`, assert no derived
    record's `longrepr`/captured text appears in the returned lines.
  - **Fallback:** if the report is empty/missing, return a single minimal redacted line
    (`"tests failed; details withheld (held-out tier)"`) — **never** raw pytest stdout.

### `src/jaunt/generate/codex_backend.py` (Task PROMPTS)
- In `_build_prompt` (currently ~line 391), append a held-out role section to `blocks`,
  branched on `ctx.kind`:
  - `ctx.kind == "test"` → **Tester** section: the Implementer sees only redacted
    pass/fail, so your suite is the sole gate — make derived cases adversarial, not
    mirrors; **derive every expected value from the contract, never from observed
    behavior** (precommit it); **tag every test** `@pytest.mark.jaunt_tier("example")`
    (asserts a docstring canonical example) or `@pytest.mark.jaunt_tier("derived")`
    (your own case); **name derived cases opaquely** (`test_derived_01`, not
    `test_empty_list_returns_zero`).
  - otherwise (build/implementation pass) → **Implementer** section: a separate Tester
    writes the tests; you will never see them; on repair you get derived failures as
    `{case-id, exception-class}` with no expected values, by design; don't probe or
    pattern-match to hidden cases — when example checks pass but derived checks fail,
    **re-read the contract for the general rule**; (rationale: closed-book exam graded
    by an independent examiner).
- Keep the section text concise (a short bulleted block). Confirm the exact build-kind
  value (anything `!= "test"`); branch accordingly. Add a one-line comment that this is
  the load-bearing prompt path (templates in `prompts/*.md` are not rendered by Codex).

### `src/jaunt/tester.py` + `src/jaunt/cli.py` (Task WIRE) — depends on HELDOUT
- **`tester.py`:** where generated tests are run via pytest, add `-p jaunt.heldout` to
  the pytest args and set `os.environ[heldout.REPORT_ENV]` to a temp path (clean it up).
  In the repair path (around `tester.py:1456-1466`), **replace**
  `repair_lines = _compact_failure_context(pytest_result.stdout, pytest_result.stderr)`
  with: load the heldout report and
  `repair_lines = heldout.build_repair_feedback(report, redact=not no_redact_derived)`.
  Keep the existing `implicated_build_modules` mapping; only the *content* of
  `initial_error_context_by_module` changes (same lines for each implicated module, as
  today). Add an invariant comment: *the Implementer must never receive held-out
  expected values; repair feedback is redacted by tier.* Thread a `no_redact_derived:
  bool = False` parameter through the relevant `tester` entry points.
- **`cli.py`:** add `--no-redact-derived` to the `test` command (default off; when set,
  log a loud warning that it defeats the held-out barrier) and plumb it into the
  tester call.

## Phases (workflow)

1. **Foundations** (parallel, disjoint files): **HELDOUT** (`heldout.py`) and
   **PROMPTS** (`codex_backend.py`).
2. **Integration** (after HELDOUT): **WIRE** (`tester.py` + `cli.py`).
3. **Tests** (after 1–2): **TESTS** — new `tests/test_heldout.py` (plugin
   classification: marked→tier, unmarked/unknown→derived; opaque-id stability;
   redactor: example→full, derived→`{id, exc}`, collection→derived; `redact=False`
   full; empty-report fallback is redacted not raw) + repair-wiring test (mocked pytest
   failure ⇒ regen error context is redacted, contains no `assert`/expected-value text
   for derived) + a `_build_prompt` content test (build kind has Implementer section;
   test kind has Tester section) + a `--no-redact-derived` cli test.
4. **Verify** (one agent): `uv run ruff check .`, `uv run ty check`, `uv run pytest`;
   drive `codex@high` to fix failures until green (bounded, max ~3 rounds). Report
   status; do **not** commit.
5. **Codex review** (one agent, read-only): `codex exec -s read-only` reviewing the
   changed files against the design doc — focus on residual leak channels, the
   fail-safe-to-derived default holding for collection/error phases, and the redactor
   never falling back to raw stdout. Return findings as free text.

## Acceptance criteria

- A `derived`-tier test failure surfaces to the Implementer as `{opaque-id,
  exception-class}` only — no expected/actual, traceback, input, or descriptive name.
  An `example`-tier failure keeps full detail. An untagged or collection-phase failure
  is treated as `derived`.
- The repair path no longer feeds raw pytest stdout to the build regeneration; an
  empty/missing report yields a redacted fallback line, never raw output.
- `_build_prompt` emits the Implementer section on the build pass and the Tester section
  on the test pass (`ctx.kind == "test"`).
- `--no-redact-derived` produces full detail for all tiers and logs a loud warning.
- `uv run ruff check .`, `uv run ty check`, `uv run pytest` all green.
