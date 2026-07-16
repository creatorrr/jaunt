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

## Cycle 8 — 2026-07-15

- [x] Unwrap TypeScript package redirect source files during compiler-program reuse.
- [x] Include the active worker phase in unexpected overlay failures.
- [x] Preserve and reuse a request-validated candidate after a later overlay transaction failure.
- [x] Add regressions and run the full repository validation suite.

## Cycle 9 — 2026-07-15

- [x] Preserve battery reuse proof across separate TypeScript build and test commands.
- [x] Report `recomposed` modules in pure and mixed TypeScript build JSON.
- [x] Add regressions and run the full repository validation suite.

## Cycle 10 — 2026-07-15

- [x] Scope targeted TypeScript contract responses and validation to the explicit and public-import closure.
- [x] Keep targeted test generation and magic battery checks on the same scoped contract path.
- [x] Preserve the worker's public-import closure through the Python bridge and retain ambient declarations.
- [x] Traverse ordinary barrels/context files and retain regular `.ts` global declarations.
- [x] Bound full-workspace contract responses and batch sync validation in dependency order.
- [x] Commit independent successful TypeScript candidates when one sibling fails.
- [x] Revalidate each independent landing unit against the committed sibling baseline.
- [x] Make unbuilt placeholders safe under strict unused checks.
- [x] Exclude ordinary co-located native tests from production import provenance.
- [x] Fix mixed `clean --orphans` preflight argument defaults.
- [x] Make plugin status timeouts informative, preserve TS diagnostics, and avoid duplicate mixed probes.
- [x] Refresh both plugin versions so adopters receive the fail-open hook launchers.
- [x] Add regressions and run the full repository validation suite.

## Cycle 11 — 2026-07-15

- [x] Retry final TypeScript unit-conformance failures with the rejected candidate and exact diagnostics.
- [x] Report per-module attempt counts, retry phases, and deduplicated reasons in JSON.
- [x] Keep Codex generation hermetic so user plugins and hooks do not inflate target prompts.
- [x] Prune implementation-only imports from strict API mirrors and isolate independent sync batches.
- [x] Batch status overlays, release compiler state between batches, and make worker heap/OOM behavior explicit.
- [x] Preserve public optionality in generated private helper types and add real-worker regressions.
- [x] Document TypeScript candidate outcomes, retry accounting, and the 1.7.6 upgrade path.
- [x] Refresh both plugin skill bundles with conformance, OOM, and hermetic-generation guidance.
- [x] Cut Jaunt 1.7.6 / @usejaunt/ts alpha.5, run full validation, and obtain an independent Codex review.

## Cycle 12 — 2026-07-15

- [x] Preserve valid battery responses after a late peer failure and generate them with configured concurrency.
- [x] Validate every live and cached battery candidate with exact compiler and static-loader diagnostics.
- [x] Preserve a green compatible subset, evict rejected cache entries, and report each retry outcome.
- [x] Keep bounded typecheck diagnostics through the derived-output redaction boundary.

## Cycle 13 — 2026-07-15

- [x] Preserve mixed-workspace TypeScript `candidate_outcomes`.
- [x] Retry transient model-capacity failures without consuming the candidate budget or dropping partial reports.
- [x] Wire plain stderr progress through TypeScript build, test, and nested implementation-repair phases.
- [x] Update both first-party plugin guides to request live progress alongside final JSON.
- [x] Confirm live module and battery-tier progress in the adopter's narrowed recovery run.
- [x] Fix mixed `clean --orphans` preflight when the clean parser has no `jobs` attribute.
- [x] Record the narrowed retry: four batteries staged, four exhausted, with native coverage retained.
- [ ] Add a default-compatible per-intent tier selection after the stable release.
- [ ] Decide whether to include bounded imported-type context after preserving or intentionally invalidating the 12 recovered cache entries.
- [ ] Confirm the final stable-release scope.
