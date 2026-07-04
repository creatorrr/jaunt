# Changelog

All notable changes to jaunt. Generated from conventional commits by
[git-cliff](https://git-cliff.org); one section per published PyPI release.

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
