# Changelog

All notable changes to Jaunt and `@usejaunt/ts`. Generated from conventional
commits by [git-cliff](https://git-cliff.org), with one section per published
Python or TypeScript release.

## [1.7.10 / @usejaunt/ts 0.1.1] - 2026-07-21

### Fixes

- Exclude the raw text segments of tagged template literals from TypeScript
  semantic-environment freshness. Editing a GraphQL `gql` document or another
  tagged string in a transitive application import no longer restales governed
  modules and their generated batteries when the TypeScript contract is
  unchanged. Substitution expressions and ordinary untagged template literals
  remain part of the structural digest.

### Packages

- Publish `@usejaunt/ts` 0.1.1 with the tagged-template freshness correction.
  Jaunt 1.7.10 carries the matching worker behavior and upgrade guidance.

## [1.7.9 / @usejaunt/ts 0.1.0] - 2026-07-15

### Fixes

- Recover TypeScript implementations whose saved semantic-environment identity
  changed without changing their authored contract. `jaunt migrate --language
  ts` now validates the existing candidate against the current worker,
  compiler, static policy, public API, and consumer closure, then reports a
  model-free `free-recompose` action. Contract, symbol, option, dependency, and
  failed-overlay changes still require a rebuild.
- Scope semantic-environment compatibility to the resolved declaration closure
  instead of hashing an entire package-manager lockfile. New sidecars retain
  package- and workspace-scoped digests so status and migration JSON can
  identify added, removed, and changed declaration records without embedding
  hundreds of individual library files.
- Treat the root `package.json#packageManager` selector as tooling provenance,
  not part of the model-facing semantic contract. It still triggers structural
  inspection, is reported as an exact tooling record, and qualifies for a
  compiler-validated model-free recomposition when declarations are unchanged.
- Preserve the validated old-to-new API reuse proof when migration recomposes an
  implementation. A later `jaunt test --language ts --no-build` can reheader
  compatible batteries without paid regeneration; the artifact transaction
  remains atomic and green-gated.
- Validate the complete multi-module recomposition set before committing its
  atomic transaction. Migration also validates mutually imported unbuilt
  placeholders together and refuses `--apply` while any module still requires
  a rebuild.
- Release worker compiler programs between migration modules and on thrown
  validation failures, keeping broad model-free recovery bounded on large
  workspaces.
- Report the running Jaunt version, entrypoint, module, Python executable,
  distribution/editable source, nearest `uv.lock`, and locked Jaunt requirement
  from `jaunt doctor --json`. A running/locked version mismatch gets an explicit
  warning before `uv sync` can replace the active checkout.
- Validate every live TypeScript test candidate with the protected runner's
  project overlay and static-loader policy, not just cached responses. Compiler
  and `JAUNT_TS_TEST_DYNAMIC_LOADER` diagnostics now enter the remaining retry
  budget, and test JSON reports attempts, retry counts, and exact retry reasons
  per battery.
- Preserve compiler and policy messages for non-executing typecheck retries,
  bounded to 2,000 characters by the Python protection layer. Executed derived
  test failures keep their opaque case/category boundary.
- Isolate a deterministic compatible subset when separately valid batteries
  conflict in the combined overlay. Jaunt caches and runs that subset, commits it
  only when the subset is green, rejects the conflicting paths, and still exits
  `3` for the incomplete run.
- Evict a cached response when its current validator or combined-subset check
  rejects it. Compare-and-delete keeps a concurrently written replacement, and
  per-battery JSON reports whether a subset-rejected cache entry was removed.
- Wire TypeScript build and test progress through standalone and mixed CLI paths.
  Explicit `--progress plain --json` now prints the active module or battery,
  tier, retry phase, and completion count to stderr while stdout remains one JSON
  object. Test-triggered initial builds and held-out implementation repairs keep
  using the same reporter instead of going silent after battery generation.
- Retry the known Codex capacity response twice with short backoff, outside the
  candidate attempt budget. If capacity remains exhausted, TypeScript returns a
  per-module or per-battery infrastructure failure and keeps completed battery
  outcomes and validated cache entries in the same report.
- Preserve TypeScript `candidate_outcomes` in mixed-workspace build JSON and in
  the language-local `targets.ts` partition.
- Keep mixed-workspace `clean --orphans` Python preflight independent of the
  clean parser's argument shape. Workspaces with Python specs no longer crash
  because the status-only `jobs` default is absent.
- Evict an identifiable cached battery when a compatible-subset or `--no-build`
  final protected Vitest run rejects it. The next retry regenerates that battery
  instead of replaying a known behavior failure; unrelated validated cache
  entries remain.

### Packages

- Graduate `@usejaunt/ts` from its alpha line to stable `0.1.0` on the npm
  `latest` dist-tag. The Node 20–24 and TypeScript 5.8–6.x support ranges are
  unchanged.
- Make `jaunt init --language ts` install the compatible `@usejaunt/ts@^0.1.0`
  line instead of the prerelease `next` channel.
- Publish Codex plugin 1.1.6 and Claude Code plugin 1.2.6 with stable TypeScript
  target wording.
- Limit SessionStart freshness to the nearest active Jaunt workspace when one
  exists. Nested examples and independently runnable child workspaces no longer
  flood a root session; bounded descendant discovery remains available when the
  session has no parent `jaunt.toml`.

### Documentation

- Refresh the package README, repository guide, Fumadocs quickstart, limits,
  upgrade notes, and release instructions for the stable npm channel.
- Document the dry-run/apply/check recovery sequence for environment-only
  TypeScript provenance drift in the CLI, upgrade guide, package README, and
  both first-party plugins.
- Have both first-party plugin guides request plain stderr progress for long
  JSON build and test commands, while preserving stdout for the final payload.
- Bump the matching worker protocol and contract IR to draft 3 for
  per-environment records and explicit compiler-program release. Migration
  recognizes the draft-2 to draft-3 artifact transition as model-free when the
  saved contract passes current validation. The npm package is stable, but the
  wire protocol is not yet a public stability promise.

## [1.7.8] - 2026-07-15

### Fixes

- Accept unused parameters only in canonical TypeScript `jaunt.magic()` marker
  stubs, including dependency-closure specs compiled during targeted sync. Strict
  projects keep `noUnusedParameters` enabled without hiding the same diagnostic in
  preserved or otherwise handwritten code.
- Label the embedded `jaunt instructions` freshness snapshot by target. Mixed
  workspaces no longer present Python-only counts as whole-workspace freshness and
  point to TypeScript status for the authoritative TypeScript result.
- Run TypeScript example and derived battery generation with the configured test
  job limit. Completed responses are staged in the cache after a combined compiler
  preflight, including the valid partial set before returning a late request failure,
  so retries avoid repeating successful paid calls without retaining a conflicting
  battery set. Artifact files still land only after the complete set passes.

### Packages

- Publish `@usejaunt/ts` `0.1.0-alpha.7` with narrow marker-stub parameter handling
  and parallel resumable battery generation.

## [1.7.7] - 2026-07-15

### Fixes

- Preserve handwritten-consumer diagnostics when full TypeScript status and check
  validate sync work in bounded batches.
- Seed scoped reverse-consumer validation from the modules whose candidate or API
  mirror actually changes, rather than unrelated dependencies loaded into the
  validation closure.
- Prune API-mirror imports shadowed by value parameters in public `typeof`
  declarations as well as imports shadowed by generic type parameters.

### Packages

- Publish `@usejaunt/ts` `0.1.0-alpha.6` with the scoped validation and mirror
  pruning corrections.

## [1.7.6] - 2026-07-15

### Fixes

- Feed final TypeScript unit-conformance failures back into the remaining generation
  budget. The rejected candidate is retained as the repair seed, same-module private
  imports and TS2322 optionality drift receive exact diagnostics, and build JSON reports
  per-module attempts, retry counts/reasons, and terminal phase.
- Run paid Codex generation without user-level Codex configuration. Project-seeded
  skills and explicit Jaunt Codex settings remain available, while unrelated MCPs,
  plugins, and hooks no longer inflate target prompts or fail inside generation. Older
  Codex CLIs that reject the hermetic flag fall back once before any model call.
- Prune type and runtime imports that are not used by the public TypeScript declaration
  surface. Strict `noUnusedLocals` projects no longer reject API mirrors for dependencies
  needed only by the eventual implementation.
- Validate full-workspace TypeScript status and sync operations in scoped dependency
  batches, deduplicate repeated diagnostics, and attribute sync failures to the owning
  module without poisoning independent batches. Candidate landing scopes retain reverse
  handwritten consumers so a proposed public API cannot land while breaking application
  code; model-free target bootstrap remains isolated from unrelated consumers.
- Charge every TypeScript repair attempt as a distinct API call and enforce the cost
  ceiling between attempts instead of only after their aggregate returns.
- Release compiler Program state between scoped overlay batches. A validated
  `[target.ts].worker_heap_mb` setting provides a supported Node heap override, and a
  deterministic heap OOM is reported once with the active request instead of replayed.

### Packages

- Publish `@usejaunt/ts` `0.1.0-alpha.5` with strict mirror pruning and bounded overlay
  compiler state.
- Publish Codex plugin `1.1.4` and Claude Code plugin `1.2.4` with
  attempt-outcome, deterministic-OOM, and hermetic-generation guidance.

## [1.7.5] - 2026-07-15

### Fixes

- Preserve compiler-validated TypeScript API reuse proof across separate
  `jaunt build` and `jaunt test` commands. Matching generated Vitest batteries
  are reheadered and rerun without another model call, including after a later
  metadata-only restamp.
- Keep `recomposed` TypeScript modules in pure and mixed-workspace build JSON.
- Keep targeted TypeScript test generation and magic battery checks on the selected
  contract closure.
  `refrozen` remains the umbrella for every model-free reuse path, while
  `recomposed` identifies the compatible-toolchain subset.
- Make targeted TypeScript contract responses and overlay validation follow
  only the selected modules and their explicit or public-import closure,
  including ordinary barrels/context files and configured global declarations.
  Unrelated project errors and unbuilt sibling placeholders no longer force
  adopters to rotate roots or `tsconfig` includes between builds.
- Split full-workspace contract analysis into bounded responses and deterministic
  sync validation into dependency-ordered batches. Artifact writes remain one
  atomic transaction, while large workspaces no longer need one oversized worker
  response.
- Isolate independent TypeScript build transactions even when modules share a
  package owner or project-reference graph. A failed candidate no longer aborts
  successful siblings; each successful unit is revalidated against the
  committed baseline before it lands, and only explicit dependency-connected
  modules stay atomic.
- Emit strict-unused-safe TypeScript placeholders and exclude ordinary
  co-located `*.test.ts[x]` and `*.spec.ts[x]` files from production dependency
  provenance.
- Fix mixed-workspace `clean --orphans` preflight when its parser does not carry
  status-only flags.
- Make plugin health probes use one status request per mixed workspace without
  dropping TypeScript diagnostics, give analysis a longer bounded window, and
  report timeouts as status failures instead of claiming that the worker or
  compiler is unavailable.

### Packages

- Publish `@usejaunt/ts` `0.1.0-alpha.4` with scoped analysis, bounded sync,
  isolated build transactions, strict-safe placeholders, and native-test
  provenance fixes.
- Publish Codex plugin `1.1.3` and Claude Code plugin `1.2.3`, including the
  fail-open lifecycle launchers from PR #89 and the updated health probes.

## [1.7.4] - 2026-07-15

### Fixes

- Unwrap TypeScript package redirect source files before reusing them in a new
  compiler program. Consecutive overlay validation now works in pnpm and other
  projects where duplicate package identities produce compiler redirects.
- Include the active worker phase in unexpected analyzer errors. An
  implementation candidate accepted before a later transaction failure remains
  cached and is revalidated on a same-project retry without another model call.

### Packages

- Publish `@usejaunt/ts` `0.1.0-alpha.3` with safe compiler-program reuse and
  phase-attributed internal errors.

## [1.7.3] - 2026-07-14

### Fixes

- Make TypeScript worker request and startup deadlines configurable, and include
  phase timings plus the relevant config hint when a worker request times out.
- Revalidate and deterministically recompose existing TypeScript candidates when
  a later toolchain upgrade changes only Jaunt metadata or composition details.
  Alpha.2 sidecars persist a normalized environment identity; the proof covers each
  module's dependency closure, while older sidecars conservatively rebuild once.
  Matching Vitest batteries are reheadered without model calls.
- Correct the TypeScript guide: failed infrastructure runs commit neither generated
  batteries nor generation-cache entries.

### Packages

- Publish `@usejaunt/ts` `0.1.0-alpha.2` with deterministic candidate recomposition
  and worker phase telemetry.

## [1.7.2] - 2026-07-14

### Fixes

- Format emitted provenance stubs with the owning project's Ruff configuration,
  then record their rendered bytes so `jaunt status` and `jaunt check` detect
  formatter or manual drift without a model call.
- Expand the human-readable generation plan with each module's split-component
  fan-out, maximum attempts, monolithic fallback, and fallback conditions.
- Keep generated TypeScript batteries out of the implementation-orphan scan when
  source and test roots overlap.
- Preserve exported TypeScript function names and accept strict unused checks in
  private specs and worker conformance files.
- Treat Vitest collection, timeout, runner, and protocol failures as
  non-repairable runner errors. They no longer trigger implementation repair,
  and failed runs do not commit generated candidates or cache entries.
- Add a TypeScript-only auto-skills switch, a read-only skill footprint plan, and
  `.jaunt-vitest-cache/` to generated `.gitignore` entries.
- Resolve same-package TypeScript path aliases as local imports, scope targeted
  diagnostics to selected modules, and collapse repeated provenance errors.

### Packages

- Publish `@usejaunt/ts` `0.1.0-alpha.1` with the TypeScript worker fixes in this
  release.

## [1.7.1] - 2026-07-14

### Fixes

- Normalize Jaunt-authored Python with Ruff after generation, while preserving
  narrow lint accommodations for generated implementation fragments and stubs.
- Keep targeted builds, tests, status, and progress reporting scoped to the
  requested module and its owning project.
- Improve generated stub fidelity for imports, async context managers, and
  workspace-routed modules.
- Attribute model work and cost consistently across retries, failures,
  interrupts, and generated test namespaces.

### Tooling

- Ship Ruff as a runtime dependency and use `ruff format` followed by
  `ruff check --fix --unsafe-fixes` for Jaunt-authored Python.
- Add a native `jaunt doctor` command and route the Codex and Claude plugin
  doctor workflows through it.

## [1.7.0] - 2026-07-13

### Features

- Add the first alpha TypeScript target. Private `*.jaunt.ts[x]` specs are
  analyzed without execution by the project-local `@usejaunt/ts` worker, then
  generated implementations are checked against the owning TypeScript project
  before Jaunt writes them.
- Add deterministic `jaunt sync` and TypeScript migrations, declaration design,
  Vitest batteries, contract mode, project-reference support, and the ordinary
  lifecycle commands (`build`, `test`, `status`, `check`, `watch`, daemon jobs,
  `clean`, and `eject`) under stable `ts:` target identities.
- Add version-2 configuration for Python-only, TypeScript-only, and mixed
  workspaces. Existing version-1 Python projects keep their configuration and
  output layout.

### Release

- Add coordinated, exact-artifact release checks for the Python distribution
  and the independently versioned `@usejaunt/ts` npm package.

## [1.6.3] - 2026-07-13

### Chores

- Update CHANGELOG.md for v1.6.2 [skip ci]

### Other

- [codex] fix unsafe removal-only restamps and release 1.6.3 (#78)
- [codex] add Codex plugin and refresh Claude plugin (#77)
## [1.6.2] - 2026-07-10

### Chores

- Update CHANGELOG.md for v1.6.1 [skip ci]

### Other

- [codex] add per-module workspace routing (#76)
## [1.6.1] - 2026-07-10

### Chores

- Update CHANGELOG.md for v1.6.0 [skip ci]

### Other

- [codex] Default Jaunt generation to gpt-5.6-sol (#75)
## [1.6.0] - 2026-07-07

### Chores

- Update CHANGELOG.md for v1.5.2 [skip ci]

### Docs

- Prominent self-hosting page + landing section — Jaunt builds Jaunt (#73)

### Features

- Contract-mode `properties` case kind (Hypothesis-backed) — design + first cut (#74)
## [1.5.2] - 2026-07-05

### Chores

- Update CHANGELOG.md for v1.5.1 [skip ci]

### Features

- 1.5.2 — jaunt builds jaunt (self-hosting bootstrap) + install-claude-plugin (#72)
- Claude Code plugin 1.0.0 — guard + freshness hooks, build/doctor/convert skills, first-build reviewer (#71)
## [1.5.1] - 2026-07-05

### Chores

- Update CHANGELOG.md for v1.5.0 [skip ci]

### Fixes

- 1.5.1 — wave-6 adoption findings 27/28/29 (workspace import policy, multi-root gate, free fingerprint re-stamp) (#70)
## [1.5.0] - 2026-07-05

### Chores

- Update CHANGELOG.md for v1.4.2 [skip ci]

### Features

- 1.5.0 — advisories channel, jaunt migrate, orphan lifecycle, skills trust-and-verify (#69)
## [1.4.2] - 2026-07-04

### Chores

- Update CHANGELOG.md for v1.4.1 [skip ci]

### Fixes

- 1.4.2 — wave-4 adoption feedback (#68)
## [1.4.1] - 2026-07-04

### Chores

- Update CHANGELOG.md for v1.4.0 [skip ci]

### Fixes

- 1.4.1 — magic_module post-merge review fixes (#67)
## [1.4.0] - 2026-07-04

### Chores

- Update CHANGELOG.md for v1.3.1 [skip ci]

### Docs

- Plan amendment — highlight parallel DAG builds + change detection (Tasks 8-9), token-savings FAQ framing
- Magic_module implementation plan (1.4.0)
- Magic_module spec — fold in codex@high design review (2 P1, 4 P2, 2 P3)
- Magic_module design spec (module-level magic, 1.4)
- Wave-3 adoption feedback — patch-compat promise, @sig migration restale note, characterization-tests gate

### Features

- 1.4.0 — jaunt.magic_module, module-level magic as the primary style (#66)
## [1.3.1] - 2026-07-04

### Chores

- Update CHANGELOG.md for v1.3.0 [skip ci]

### Docs

- Jaunt.ing overhaul — Diátaxis-lite IA, executed tutorials, landing page (#64)
- Jaunt.ing overhaul implementation plan
- Jaunt.ing overhaul design spec (Diátaxis-lite)

### Fixes

- 1.3.1 — adoption feedback wave 2 (findings 18, 21, 22 polish) (#65)

### Other

- Auto-fix ruff lint and format
## [1.3.0] - 2026-07-04

### Chores

- Update CHANGELOG.md for v1.2.0 [skip ci]

### Features

- 1.3.0 — adoption-feedback improvements (guardrails, strict config, @sig, .pyi stubs, CI-grade check) (#63)
## [1.2.0] - 2026-07-04

### Chores

- Update CHANGELOG.md for v1.1.0 [skip ci]

### Features

- Propose-only daemon landing (new default) + discovery AST prescreen (#62)
## [1.1.0] - 2026-07-03

### Chores

- Update CHANGELOG.md for v1.0.0 [skip ci]

### Docs

- Sync docs-site with v1.0.0 — drop removed features, cover new ones (#60)

### Features

- Class interface guideposts + inheritance-aware whole-class magic (#61)
## [1.0.0] - 2026-07-03

### Chores

- Update CHANGELOG.md for v1.0.0rc8 [skip ci]

### Fixes

- Mirror package sources into the ty sandbox + bump to 1.0.0 (#59)
## [1.0.0rc8] - 2026-07-02

### Features

- Agent-friendly progress (--progress) + jaunt jobs wait (#57)

### Fixes

- Commit brand-new CHANGELOG.md (git diff is blind to untracked files) (#58)
## [1.0.0rc7] - 2026-07-02

### Chores

- Pre-1.0 cleanup — drop MCP server + Claude plugin, add jaunt specs, laws in preamble, changelog automation (#55)
- Drop dead code (_BATTERY_DIGEST_FIELDS, _func_node)
- Add Claude Code GitHub Action workflow (#38)
- Add PyPI publish workflow with OIDC trusted publishing

### Docs

- 6 core laws + named corollaries (#54)
- Principles-doc sync + jaunt daemon design & plan (v1.0.0rc4) (#52)
- Migrate docs-site to the Codex engine + batteries-included install
- Refresh examples/CLAUDE/README for Codex (fix broken rich_tictactoe, add [skills]/jaunt[test], list new examples)
- Codex-harness reflects the codex exec pivot + gpt-5.5
- DX Wave 1 + skills plan (dogfood + grounded Codex review)
- Add Ultimate Tic-Tac-Toe example + dogfood case study
- Record mcp-server→codex exec pivot + gpt-5.5 in the design spec
- Codex engine docs; whole-class example drops legacy pin
- Design + plan + codex-harness skill for the Codex engine cutover
- Harden whole-class-aider plan with codex review (9 findings) + align spec fallback stamping
- Add whole-class-aider implementation plan
- Harden whole-class-aider design with codex review findings
- Document [contract] config and contract exit-code behavior
- Design for whole-class @magic under the aider engine
- Add Contract mode design spec + implementation plan
- Implicit auto-testing example and guide
- Whole-class @magic example and guide
- Implementation plan for whole-class @magic + auto-testing
- Add auto-testing design for whole-class @magic
- Add @jaunt.preserve override to whole-class @magic design
- Design for whole-class @magic authoring mode
- Add comprehensive Aider coding agent skill reference
- Document @magic decorator class method support and add task board example
- Docs
- Polish content — full JWT spec, iterating section, expanded limitations
- Revamp docs IA and add reader-first flow
- Docs

### Features

- Adoption parity — async functions, whole classes, and the fixture seam for contract mode (#56)
- Jaunt daemon — background codegen jobs, JAUNT_LOG journal, guard hook (v1.0.0rc5) (#53)
- Jaunt preamble + opt-in model-written project overview (v1.0.0rc2) (#50)
- Smart change detection + `jaunt instructions` agent primer (#49)
- Default Codex builder skills via seed + native discovery (#47)
- Repo-context subsystem (treedocs repo map for Codex prompts + opt-in colgrep retrieval) (#46)
- Make jaunt batteries-included
- Auto-skills kill switch + caps + selective injection (slim prompts)
- Scaffold an example spec in jaunt init + README sample
- Preflight pytest before test generation + jaunt[test] extra
- One-line build/test success summaries
- Retry once on model-config errors (e.g. verbosity)
- Default model gpt-5.5
- Codex is the sole engine — remove aider + legacy backends
- Defer jaunt eval under Codex; scaffold [codex] in init
- CodexExecutor and Codex-driven auto-skill generation
- Seed scaffold/contract into ctx + class validator in retry loop
- Carry scaffold seed + whole-class contract on ctx and cache key
- Scaffold builder, import collector, whole-class contract renderer
- Unfilled-stub, docstring-only, attribute guards for whole-class
- CodexBackend on mcp-server + [codex] config (selectable engine)
- Runnable contract-mode example + docs (Tasks 15-16)
- Status reports contract state, strength, and review cascade
- Jaunt eject (off-ramp) -> plain Python + plain pytest
- Model-backed derivation fallback for unstructured prose
- Jaunt adopt (on-ramp) + marker source edits
- Jaunt reconcile (deterministic derivation + strength)
- Jaunt check + mutation strength scoring (Tasks 8-9)
- Battery file format + deterministic derivation (Tasks 5-6)
- Foundations — decorator/registry, config, digests, header, drift (Tasks 1-4,7)
- Class-aware test prompt and white-box fragility warning
- Source class test API from generated impl; track generated-API staleness
- Synthesize virtual baseline test specs for @magic(test=True) classes
- [test] auto_class_tests config flag
- Hybrid generated-class API summary + public-API digest
- Whole-class build branch with class validator and base-class contract
- Whole-class specs depend on project-spec base classes
- Class-aware build validator (structure, abstractmethods, preserved-intact, docstring)
- Resolve base-class/MRO contract for whole-class specs
- Accept @magic(test=...) kwarg for opt-in implicit tests
- Add and export @jaunt.preserve identity decorator
- Class-analysis util (stub heuristic, mode detection, member split)
- Better parallelism, v0.4.1 (#42)
- Make aider the default runtime (#41)
- Add aider-backed agent runtime (#40)
- Enhance skill CLI with aliases, lib bootstrapping, and LLM build (#37)
- Support @magic decorator on class methods
- Publish
- Add eval cli and snapshot tests

### Fixes

- Targeted build stays in closure; codex fingerprint drops unused templates
- @jaunt.magic/@test accept bare and called forms
- Surface real codex exec failures + parse cached tokens
- Clean jaunt init [llm] template + stale mcp-server refs
- Drive codex exec instead of mcp-server; whole-class build works E2E
- Whitespace-tolerant docstring-retention check; pin example to legacy engine
- Fix 0.3.2 error jaunt.cli not found
- Resolve ty type-check errors in digest, runtime, and tests
- Unwrap classmethod/staticmethod descriptors in method wrapper
- Fix-examples

### Other

- Held-out tests: implementer/tester independence — L14 principle + jaunt impl (#51)
- Coding-agent principles framework + verified jaunt fixes (#48)
- Auto-fix ruff lint and format
- 1.0.0rc1
- Prepare 0.4.4 release (#45)
- Add all-sdk extra and document aider gotcha (#44)
- Make aider blueprint reference-only and reduce retry loops (#43)
- Add Claude Code GitHub Workflow (#39)
- Add decorator metadata context for magic specs (#36)
- Merge pull request #34 from creatorrr/claude/document-magic-decorator-k313S
- Merge pull request #35 from creatorrr/claude/add-pypi-publish-action-mgTAw
- Merge pull request #33 from creatorrr/claude/magic-decorator-class-methods-Nfb52
- Auto-fix ruff lint and format
- Merge branch 'claude/magic-decorator-class-methods-Nfb52' of http://127.0.0.1:52316/git/creatorrr/jaunt into claude/magic-decorator-class-methods-Nfb52
- Auto-fix ruff lint and format
- Merge remote-tracking branch 'origin/main' into claude/magic-decorator-class-methods-Nfb52
- Merge pull request #32 from creatorrr/claude/add-agent-docs-29MJ9
- Add AGENTS.md and CLAUDE.md to every __generated__/ directory
- Auto-fix ruff lint and format
- Merge pull request #31 from creatorrr/claude/add-async-decorator-support-kBwKt
- Add AGENTS.md as symlink to CLAUDE.md
- Add pre-commit hook for ruff lint+format and ty type check
- Fix type error: cast async gen_fn return to Awaitable[object]
- Auto-fix ruff lint and format
- Add async function support to @magic and @test decorators
- Merge pull request #29 from creatorrr/claude/plan-jaunt-plugin-Cmky7
- Fix Claude plugin structure for proper installation
- Auto-fix ruff lint and format
- Bump version to 0.3.0 for PyPI release
- Add implementation plan for Jaunt Claude Code plugin
- Add Jaunt Claude Code plugin with skills, hooks, and MCP integration
- Merge pull request #27 from creatorrr/claude/add-claude-skill-zhyP5
- Add Claude skill for Jaunt spec-driven code generation workflow
- Merge pull request #28 from creatorrr/claude/claude-plugin-skill-hX3ae
- Add claude-plugin-skill for building Claude Code plugins and skills
- Merge pull request #26 from creatorrr/feat/reasoning-controls-and-eval-results
- Auto-fix ruff lint and format
- Add reasoning controls across providers and publish eval results
- Merge pull request #25 from creatorrr/claude/add-cerebras-provider-lX3t0
- Add cerebras-cloud-sdk to dev dependencies for ty typecheck
- Auto-fix ruff lint and format
- Add Cerebras inference provider using cerebras-cloud-sdk
- Merge pull request #24 from creatorrr/claude/investigate-naming-reference-3onjE
- Add Blake's Tyger plate illustration to README and docs-site
- Add Bester/Blake literary references to README, docs, and module docstrings
- Merge pull request #23 from creatorrr/feat/eval-ty-retry-followup
- Auto-fix ruff lint and format
- Address builder review cleanup and ty timeout handling
- Add in-loop ty retry feedback for build generation
- Merge pull request #21 from creatorrr/feat/task-140-eval-suite
- Merge pull request #22 from creatorrr/copilot/review-pr-21
- Initial plan
- Add eval subprocess timeouts and skip-on-missing-ty
- Ignore .jaunt artifacts and remove TASK-140 planning file
- Auto-fix ruff lint and format
- Add eval suite CLI, built-in cases, and prompt snapshots
- Merge pull request #19 from creatorrr/docs/fumadocs-sync-last-15-commits
- Sync Fumadocs with recent CLI/provider/MCP changes
- Merge pull request #20 from creatorrr/claude/cleanup-tasks-docs-o9Z8T
- Remove 13 completed TASK files
- Remove completed planning docs
- Merge pull request #18 from creatorrr/claude/review-tasks-list-4cxzJ
- Auto-fix ruff lint and format
- Fix type errors in cost estimation and cache tests
- Add LLM response caching, cost tracking, and budget limits (TASK-130)
- Merge pull request #17 from creatorrr/fix/remove-mcp-config-watch-json
- Auto-fix ruff lint and format
- Remove MCP enabled config and simplify watch JSON output
- Merge pull request #16 from creatorrr/claude/implement-next-task-tdd-XyNys
- Auto-fix ruff lint and format
- Fix MCP server review issues: root passthrough, JSON validation, sys.path cleanup
- Add MCP server for agent-friendly programmatic interface (task 110)
- Merge pull request #15 from creatorrr/fix/watch-clean-status-correctness
- Fix watch --test, scope clean roots, and print status output
- Merge pull request #14 from creatorrr/claude/implement-next-task-tdd-7eajw
- Mark TASK-090 as done
- Make all LLM SDKs optional dependencies (task 090)
- Merge pull request #13 from creatorrr/claude/review-recent-commits-sLIqn
- Fix examples: add missing stub bodies and update API key references
- Auto-fix ruff lint and format
- Review and fix documentation, design issues, and a latent bug
- Merge pull request #12 from creatorrr/claude/merge-main-resolve-conflicts-bG7Tu
- Remove implementation plan document before merge
- Merge remote-tracking branch 'origin/main' into claude/merge-main-resolve-conflicts-bG7Tu
- Merge pull request #11 from creatorrr/claude/next-task-tdd-ysU9k
- Fix ty check: add type: ignore for optional watchfiles import
- Add structured output for LLM generation backends (task 120)
- Merge pull request #10 from creatorrr/claude/next-task-tdd-H4iPX
- Auto-fix ruff lint and format
- Add watch mode (TASK-100): auto-rebuild on spec file changes
- Merge pull request #9 from creatorrr/claude/next-task-tdd-DegK3
- Add error diagnostics with actionable hints (task 080)
- Merge pull request #8 from creatorrr/claude/next-task-tdd-H7BvF
- Improve prompt quality for LLM code generation (task 070)
- Merge pull request #6 from creatorrr/claude/cli-ergonomics-060-v08dR
- Add init/clean/status CLI commands (task 060)
- Merge pull request #7 from creatorrr/claude/install-mcp-server-skill-ufPxE
- Add fastmcp-mcp-server skill for building MCP servers
- Merge pull request #5 from creatorrr/claude/production-ready-refactor-xqGxP
- Add TASK-*.md DAG for production roadmap
- Add improvements list
- Update uv.lock after removing pydantic from required deps
- Production-ready refactor: fix bugs, add resilience, agent-friendliness
- Auto-fix ruff lint and format
- Fix dependency tracing: efficiency, inference accuracy, and correctness
- Merge pull request #3 from creatorrr/claude/consolidate-examples-cleanup-rV1I1
- Consolidate all examples into single directory and clean up
- Merge pull request #1 from creatorrr/claude/test-openai-examples-ClrgD
- Auto-fix ruff lint and format
- Fix E501 line-too-long in test_cli_test_dependency_apis
- Add --unsafe-fixes to ruff check
- Auto-fix ruff lint/format and commit results
- Fix import ordering in cli.py (ruff I001)
- Add CI workflow for lint, typecheck, test, and examples
- Add ambitious expr_eval and diff_engine examples
- Merge pull request #2 from creatorrr/claude/revamp-jaunt-docs-3wzkP
- Add regression tests for dependency_apis in test generation
- Make jaunt-examples specs consistent with generated tests
- Fix test generation imports using magic API context
- Examples-readme
- Examples-new
- Gh-pages
- Skills-example
- Skills
- Progress
- Examples
- V01
- 041
- Prompts
- Init
