# Feedback TODO

## Cycle 1 — 2026-07-13

- [x] Ignore nested Claude and Codex managed worktrees during doctor workspace discovery.
- [x] Limit duplicate-guard checks to the host that loaded the plugin.
- [x] Add regression coverage and run the full repository validation suite.

## Cycle 2 — 2026-07-13

- [x] Emit Ruff-clean generated implementations and provenance stubs without consumer ignores.
- [x] Avoid paid rebuilds when an upgrade changes only re-stamp-safe generation metadata.
- [x] Expose retry fan-out and clarify that seeded-skill size is on-disk availability, not prompt tokens.
- [x] Preserve committed repo-map identity and avoid worktree-name churn during status/build.
- [x] Add regressions and run the full repository validation suite.

## Cycle 3 — 2026-07-13

- [x] Add a native read-only `jaunt doctor --json` command matching the plugin promise.
- [x] Give selected modules, split components, and retries distinct progress and cost identities.
- [x] Add regressions and run the full repository validation suite.

## Cycle 4 — 2026-07-13

- [x] Isolate same-named package-local test roots during discovery and import.
- [x] Detect duplicate test import identities before generation.
- [x] Keep implementation freshness stable across equivalent test-spec path moves.
- [x] Add regressions and run the full repository validation suite.

## Cycle 5 — 2026-07-13

- [x] Scope `include_target_tests` invalidation to modules with attached test intent.
- [x] Preserve the legacy default-off fingerprint for existing unaffected artifacts.
- [x] Add regressions and run the full repository validation suite.

## Cycle 6 — 2026-07-13

- [x] Run fresh and skipped targeted generated-test modules; fail visibly on zero selection.
- [x] Include the exact pytest command and captured result in JSON output.
- [x] Teach test generation about facade/generated globals, defining-module imports, and typed negative cases.
- [x] Emit type-correct stubs for async context managers and verify them with ty, mypy, and pyright.
- [x] Add regressions and run the full repository validation suite.

## Cycle 7 — 2026-07-13

- [x] Aggregate standard Python test-generation cost across workspace owners.
- [x] Record provider usage after each completed retry attempt.
- [x] Emit partial known cost in JSON when build or test is interrupted.
- [x] Add regressions and run the full repository validation suite.
