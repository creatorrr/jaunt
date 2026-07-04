# Jaunt 1.3.0 — Adoption-Feedback Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is executed by a dynamic Workflow of opus@high subagents driving `codex exec` at effort=medium; each task below is one workflow unit with its own test cycle and commit(s).

**Goal:** Fix the actionable findings (1–16; 17 is positive-only) from the mem-mcp-b adoption campaign (FEEDBACK.md) in one 1.3.0 release: generated-code guardrails, strict config, `@sig` + typing, `.pyi` emission, layout/naming fixes, cost instrumentation, auto-skills noise reduction, and CI-grade freshness (`jaunt check` gates magic drift; repo-map content decoupled from staleness).

**Architecture:** Seven clusters mapped to nine tasks in five waves. Guardrails are enforced twice (prompt rules teach the model; AST validation hard-fails and feeds the existing regeneration-retry loop). Strictness is the default everywhere per the approved spec.

**Tech Stack:** Python 3.12+, uv, pytest (mocked generator backend — no API keys), ruff (line-length 100, E/F/I/UP/B), ty.

**Spec:** `docs/superpowers/specs/2026-07-03-adoption-feedback-1.3-design.md` — read it before starting any task.

## Global Constraints

- Strict by default: unknown config → `JauntConfigError` (exit 2); generated-code violations → build errors (retry loop); no dual-path layout fallback.
- Every new config key must appear in BOTH the config allowlist (Task 2's `_reject_unknown` data) and `init_template.py`, or the Task 2 round-trip test fails.
- All existing tests must stay green in every task's commit; run `uv run pytest` (full), `uv run ruff check .`, `uv run ty check` before each commit.
- Do not touch `__generated__/` content in `examples/` by hand.
- Prompt-template edits change the build fingerprint by design (documented behavior); do not add compensating hacks.
- Commit messages: conventional commits (`feat:`, `fix:`, `docs:`, `test:`).

## Wave map

| Wave | Tasks | Parallel? |
|------|-------|-----------|
| 1 | 1 (guardrails), 2 (strict config), 3 (@sig + typing) | yes — disjoint files |
| 2 | 4 (.pyi emission), 5 (layout/naming), 6 (cost instrumentation) | yes |
| 3 | 7 (check gates magic) | after 4 |
| 4 | 8 (docs sweep) | after all features |
| 5 | 9 (integration + green gate) | last |

File-overlap warning for parallel waves: Task 2 and Task 4 both touch `config.py`/`init_template.py` — Task 4 is in wave 2, after Task 2 lands, so it must rebase on wave-1 output. Task 5 and Task 7 both touch `cli.py` in different commands (`clean`/discovery warnings vs `check`); waves keep them apart.

---

### Task 1: Generated-code guardrails (spec Cluster 1; findings 1, 7, 8)

**Files:**
- Modify: `src/jaunt/prompts/build_module.md` (deps section lines ~32-36; reuse rule line ~62)
- Modify: `src/jaunt/prompts/codex_preamble.md` (hard-rules list, near L12 "import only declared dependencies")
- Modify: `src/jaunt/validation.py` (new checks beside `_validate_build_contract_only`, lines ~153-171)
- Modify: `src/jaunt/builder.py` (NEEDS-DEP marker scan after successful validation; JSON plumbing)
- Modify: `src/jaunt/cli.py` (surface `needs_deps` in build report + `--json`)
- Test: `tests/test_validation_guardrails.py` (new), extend `tests/test_builder_io.py` or nearest builder-report test module

**Interfaces:**
- Produces: `validate_no_import_fallbacks(tree: ast.AST, protected_modules: set[str]) -> list[str]` in `validation.py` — returns error strings; wired into the same call path as `_validate_build_contract_only` (which already receives `spec_module`; the builder additionally passes declared dep modules and first-party top-level names it already knows from import-provenance inputs).
- Produces: self-import check inside `_validate_build_contract_only`: any `from <spec_module> import X` (absolute or equivalent relative) where `X ∈ expected_names` → error `"generated module re-imports its own spec symbol 'X' from <spec_module>; define it instead"`.
- Produces: `NEEDS_DEP_MARKER = "JAUNT-NEEDS-DEP:"` constant; builder collects `list[tuple[int, str]]` (lineno, marker text) per module; build result JSON gains `"needs_deps": {"<module>": ["<marker line>", ...]}` (omitted/empty when none).

- [ ] **Step 1: Failing tests first.** In `tests/test_validation_guardrails.py` write tests (follow existing `validation` test style):

```python
def test_import_fallback_around_spec_module_rejected():
    src = textwrap.dedent("""
        try:
            from timing import MOCK_TIMING_CALLS
        except ImportError:
            MOCK_TIMING_CALLS = []
    """)
    errs = validate_no_import_fallbacks(ast.parse(src), {"timing"})
    assert errs and "fallback" in errs[0]

def test_import_fallback_third_party_allowed():
    src = "try:\n    import ujson\nexcept ImportError:\n    ujson = None\n"
    assert validate_no_import_fallbacks(ast.parse(src), {"timing"}) == []

def test_self_import_of_spec_symbol_rejected():  # via the module-level validate entrypoint
    ...  # generated source containing `from timing import MockTimer` with expected_names={"MockTimer"} -> error

def test_needs_dep_marker_surfaces_as_build_warning():
    ...  # builder-level: generated source containing "# JAUNT-NEEDS-DEP: util.hashing:stable_hash — inlined" lands in result.needs_deps
```

Rules for the fallback check: flag an `ast.Try` only when (a) a handler catches `ImportError` or `ModuleNotFoundError` (bare `except:` counts), AND (b) the try body contains `Import`/`ImportFrom` whose resolved top-level module is in `protected_modules` OR is a relative import. Third-party absolute imports stay legal.

- [ ] **Step 2: Run new tests, verify they fail** (`uv run pytest tests/test_validation_guardrails.py -v`).
- [ ] **Step 3: Implement** the two validation checks + builder marker scan + JSON key. Wire fallback-check `protected_modules` = `{spec_module}` ∪ declared dependency top-levels ∪ first-party top-levels (builder already computes these sets for `_validate_generated_import_provenance` — reuse, don't duplicate).
- [ ] **Step 4: Prompt edits.** `build_module.md`: add to the dependency rules: "If the contract implies behavior from a module NOT listed in Dependency APIs, do not invent an import. Inline the minimal logic and mark the site with a comment: `# JAUNT-NEEDS-DEP: <module>:<name> — <one-line reason>`." Extend the reuse rule: "The generated module must define every spec symbol itself; never import a spec symbol back from `{{spec_module}}`. Call same-module sibling spec symbols by bare name — never via a module-level import of `{{spec_module}}` (it is mid-import at load time). Never wrap imports in try/except to provide fallbacks — import failures must raise, and there must never be a second, divergent implementation of a contract symbol." (finding 15 folded in) `codex_preamble.md`: add one hard rule: "No fallback implementations: import failures raise; never define an alternate implementation of a contract symbol."
- [ ] **Step 5: Full suite green, lint, typecheck, commit** (`feat: guardrails — reject import fallbacks and spec self-imports, surface JAUNT-NEEDS-DEP markers`).

### Task 2: Strict config + shared init template (spec Cluster 2; findings 2, 5)

**Files:**
- Create: `src/jaunt/init_template.py` (move `_INIT_TEMPLATE` from `cli.py:1664-1719` as `INIT_TEMPLATE`, `_INIT_SPEC_TEMPLATE` from `cli.py:1722-1741` as `INIT_SPEC_TEMPLATE`; content unchanged)
- Modify: `src/jaunt/config.py` (`load_config`, lines ~232-759)
- Modify: `src/jaunt/cli.py` (import templates from new module; delete the private copies)
- Modify: `src/jaunt/instructions/__init__.py` (`_project_block`, lines ~87-91)
- Test: `tests/test_config.py` (extend), `tests/test_cli.py` instructions tests (extend)

**Interfaces:**
- Produces: `_reject_unknown(tbl: dict, allowed: frozenset[str], where: str) -> None` in `config.py`, raising `JauntConfigError` like `unknown key 'reasoning-effort' in [semantic_gate] — did you mean 'reasoning_effort'?` (suggestion via `difflib.get_close_matches`, only when a match exists). Applied to the top-level table (allowed sections: `version, paths, llm, build, test, prompts, agent, codex, daemon, skills, contract, semantic_gate, context`) and to each section's keys. Allowlists must be derived from what `load_config` actually reads (every `if "k" in tbl` site) PLUS retained back-compat keys (`skills.max_chars_per_skill`, `skills.inject_user_skills`, full informational `[llm]` set) PLUS nested `[context.search]` handled as `context`'s `search` sub-table with its own allowlist.
- Produces: `jaunt instructions` with no project appends a `## jaunt.toml schema` section containing `INIT_TEMPLATE` verbatim in a toml code fence.

- [ ] **Step 1: Failing tests.** `test_unknown_section_rejected` (`[gate]\nmodel="x"` → `JauntConfigError` mentioning `semantic_gate`), `test_unknown_key_rejected` (`[semantic_gate]\nreasoning-effort="high"`), `test_unknown_search_key_rejected` (`[context.search]\nmax-hits=3`), `test_init_template_roundtrips` (write `INIT_TEMPLATE` to tmp `jaunt.toml` + create `src/`, `load_config` succeeds), `test_instructions_no_project_prints_schema` (output contains `version = 1` and `[paths]`).
- [ ] **Step 2: Verify failures.**
- [ ] **Step 3: Implement** (helper, allowlists, template move, instructions branch). Keep `cli.py` behavior identical for `jaunt init`.
- [ ] **Step 4: Sanity-check every `examples/*/jaunt.toml` and test fixture toml in the repo still loads** — fix any that use stale keys (that is the feature working; adjust fixtures, not the allowlist, unless the key is genuinely still read).
- [ ] **Step 5: Suite green, lint, ty, commit** (`feat: reject unknown jaunt.toml sections/keys; print config schema in pre-init instructions`).

### Task 3: `@sig` decorator + typing fixes (spec Cluster 3; finding 3 part 1)

**Files:**
- Modify: `src/jaunt/runtime.py` (overloads at ~173-189, 462-475, 540-545; new `sig`)
- Modify: `src/jaunt/__init__.py` (export `sig`)
- Modify: wherever the sealed tier is detected from AST decorators (grep for the inner-magic/sealed detection added in v1.1 — likely `discovery.py`/`validation.py`/class-spec parsing; extend the decorator-name match to accept `sig`/`jaunt.sig`, bare or zero-arg call)
- Modify: `src/jaunt/prompts/` class-build templates + `src/jaunt/instructions/primer.md` where the sealed tier is described (rename primary vocabulary to `@sig`; mention inner `@jaunt.magic` as alias)
- Test: `tests/test_runtime.py` (extend), `tests/test_typing_decorators.py` (new), whichever v1.1 test module covers sealed-method detection (extend with `@sig` variants)

**Interfaces:**
- Produces: `jaunt.sig` — usable as `@sig` or `@sig()`; any args/kwargs → `TypeError("@jaunt.sig takes no arguments")`. Runtime semantics identical to inner bare `@jaunt.magic` on a whole-class-spec method (sealed tier), including the `@property` restriction. Used outside a whole-class `@jaunt.magic` spec → the same clear error path inner `@magic` misuse produces today, with message pointing to `@jaunt.magic`.
- Produces: fixed overloads — bare `@magic` → `F`; `@magic(...)`/`@magic()` → `Callable[[F], F]` (the `-> Callable[[F], Any]` overload at runtime.py:173-174 is removed); `test`/`contract` audited to the same standard. `if TYPE_CHECKING:` block declares `magic`, `sig`, `test`, `contract`, `preserve` as signature-preserving (identity-typed) so Pyright/ty see decorated symbols unchanged.

- [ ] **Step 1: Failing tests.** Runtime: `@sig` and `@sig()` both mark a method sealed (assert same registry state as inner `@magic` — mirror the existing v1.1 sealed tests); kwargs raise; misuse-outside-class errors. Typing (`tests/test_typing_decorators.py`, checked by `uv run ty check` since it lives in the repo):

```python
from typing_extensions import assert_type

@jaunt.magic()
def f(x: int) -> str: ...
assert_type(f, type(f))  # concretely: reveal via assert_type(f("no"), str) must be a ty error… keep it simple:
def use() -> str:
    return f(1)  # must typecheck; f(1) -> str
```

Keep the typing test pragmatic: a module that ty must pass, exercising `@magic`, `@magic()`, `@magic(deps=[...])` on functions and a class, plus `Union["A", B]` usage of decorated names in type positions.
- [ ] **Step 2: Verify failures** (runtime tests fail; ty fails on the typing module pre-fix).
- [ ] **Step 3: Implement** (`sig`, overload fixes, TYPE_CHECKING branch, AST sealed-detection extension, prompt/primer vocabulary).
- [ ] **Step 4: Suite + ruff + ty green, commit** (`feat: add @jaunt.sig as canonical sealed-method marker; make decorators signature-preserving for type checkers`).

### Task 4: `.pyi` stub emission (spec Cluster 4; finding 3 part 2)

**Files:**
- Create: `src/jaunt/stub_emitter.py`
- Modify: `src/jaunt/builder.py` (emit after successful module build/write), `src/jaunt/config.py` + `src/jaunt/init_template.py` (`[build] emit_stubs = true`, allowlist + template), `src/jaunt/cli.py` (`clean` removes header-marked stubs; report emitted stubs), `src/jaunt/header.py` (reuse/extend header rendering for `.pyi` comments), freshness/status plumbing (`digest.py`/`status` path) so a missing-or-stale stub marks the module state
- Test: `tests/test_stub_emitter.py` (new), extend `tests/test_cli.py` clean tests + status/freshness tests

**Interfaces:**
- Produces: `build_stub_source(spec_source: str, generated_source: str, expected_names: set[str], header: str) -> str` — full-module `.pyi`: handwritten symbols' signatures from the spec module AST (annotations preserved, bodies `...`), spec symbols' signatures from the generated implementation AST (docstring-only classes expose their designed `__init__`/methods), module-level `__all__`/constants preserved with types where annotated. Deterministic output (stable ordering: source order).
- Produces: `is_jaunt_stub(path: Path) -> bool` (header sniff) used by `clean` and by the never-overwrite guard (existing non-jaunt `.pyi` → skip + warning in build report).
- Produces: stub staleness definition: stub header records the generated file's content digest; stale ⇔ recorded ≠ current, or stub missing while `emit_stubs=true` and module is built. Surfaced by `jaunt status` (and consumed by Task 7's `check`).

- [ ] **Step 1: Failing tests** for `build_stub_source` (docstring-only class gains designed `__init__` from generated source; handwritten function keeps annotations; decorated spec function appears undecorated with exact signature), never-overwrite guard, clean-removes-only-marked, status-flags-stale-stub.
- [ ] **Step 2: Verify failures.**
- [ ] **Step 3: Implement**; emission is best-effort per module but failures are loud build warnings, never silent.
- [ ] **Step 4: Suite + ruff + ty green** (note: emitted stubs in test fixtures may shadow fixture modules for ty — keep fixtures under `tests/` paths ty ignores or clean up in-test), **commit** (`feat: emit provenance-headed .pyi stubs from generated implementations ([build] emit_stubs)`).

### Task 5: Layout fix + naming warnings (spec Cluster 5; findings 6, 9, 12)

**Files:**
- Modify: `src/jaunt/paths.py` (`spec_module_to_generated_module` lines ~8-14, `generated_module_to_relpath` lines ~25-29)
- Modify: `src/jaunt/discovery.py` (shadow + root-doctor warnings, near `_module_name_for_file` lines ~104-124)
- Modify: `src/jaunt/config.py` or discovery call path for the root-is-package warning (root dir contains `__init__.py`)
- Test: `tests/test_paths.py` (update expectations), discovery tests (new warning tests)

**Interfaces:**
- Produces: top-level module `timing` with generated dir `__generated__` → generated module `__generated__.timing` → relpath `__generated__/timing.py`. Package member unchanged: `pkg.mod` → `pkg.__generated__.mod` → `pkg/__generated__/mod.py`. `generated_module_to_relpath` gains the first-segment-is-generated-dir case. Runtime loader and builder already resolve through these helpers — audit both call sites (`runtime.py:163`, `builder.py:89-92, 1566, 1839`) and any place that reverse-maps or globs old-form paths (e.g., clean's scan is glob-based and unaffected).
- Produces: `warn once per run` when a derived **top-level** module name is in `sys.stdlib_module_names` or in `importlib.metadata.packages_distributions()` (never `find_spec`); and when a configured source root itself contains `__init__.py`: "source root '<root>' is a package directory; module names will be bare (e.g. 'timing' not '<pkg>.timing') — you usually want the package parent". Warnings go through the existing diagnostics/warning channel, not bare `print`.

- [ ] **Step 1: Failing tests** (new mapping both directions; old expectations updated deliberately — this is the breaking change, called out in the release notes via Task 8; warning triggers incl. once-per-run dedup; no warning for package-parent roots).
- [ ] **Step 2: Verify failures. Step 3: Implement. Step 4: Full suite** — expect and fix fallout in any test fixture that hardcodes `<module>/__generated__/__init__.py`. **Commit** (`feat!: top-level specs generate into __generated__/<module>.py; warn on shadowing names and package-pointing roots`).

### Task 6: Context-cost instrumentation + auto-skills noise (spec Clusters 6a + 8; findings 4, 16)

**Files:**
- Modify: `src/jaunt/builder.py` (where the module prompt blocks are assembled/rendered), `src/jaunt/cli.py` (build summary + `--json`)
- Modify: `src/jaunt/skills_auto.py` (and `codex_executor.py` if the PyPI lookup lives there)
- Test: extend the builder/cli JSON tests + skills_auto tests

**Interfaces:**
- Produces: per-module `context_stats`: `{"<module>": {"<block>": {"chars": int, "est_tokens": int}}}` with blocks `preamble, system, module_contract, deps, package_context, repo_map, blueprint, skills_workspace` (use the names the builder already has; `est_tokens = chars // 4`). Always present in `--json`; human build summary prints one total line per built module (`context: 512k chars (~128k tok) — package_context 60%, repo_map 20%, …`) only for actually-built (non-skipped) modules.

- Produces (finding 16): auto-skills quiet path — before any PyPI lookup, detect that the imported distribution is not a PyPI install (uv-workspace member / editable / local path — check `importlib.metadata` `direct_url.json` for `dir_info`/local URL) and skip with a debug-level log line, never a warning; "Missing required heading" warnings are deduped into one summary line per build listing the affected skills.

- [ ] Steps: failing JSON-shape test + skills tests (workspace-internal dist → no PyPI request attempted, no warning emitted; N heading warnings → one summary line) → implement → suite/lint/ty → **commit** (`feat: per-block context size accounting in --json; quiet auto-skills for workspace-internal deps`).

### Task 7: CI-grade freshness — `check` gates magic drift, repo-map decoupled (spec Cluster 7; findings 11 + 14, both HIGH)

**Files:**
- Modify: `src/jaunt/digest.py` / `src/jaunt/builder.py` — wherever repo-map content currently enters the per-module build fingerprint (grep `repo_map` in the digest/fingerprint path)
- Modify: `src/jaunt/cli.py` (`cmd_check` + argparse: `--contracts-only`, `--magic-only`; `status` wording for informational repo-map drift)
- Reuse: the deterministic staleness computation behind `jaunt status` (no model call, no API key; includes upstream-API fallout and Task 4's stub staleness)
- Test: extend the check/status CLI tests + digest/freshness tests

**Interfaces:**
- Produces (finding 14, do this FIRST — `check` builds on it): repo-map **content** is removed from the per-module staleness digest. Stale reasons are only: own spec digest, transitive dep spec digests, upstream exported-API/base-API digests, prompt-template/preamble fingerprints, stub staleness (Task 4). The `[context] repo_map` on/off toggle may stay a fingerprint input; content churn must not. `status` reports repo-map drift, if at all, as an informational note that does NOT mark the module stale; `build` does not rebuild for it. Regression test is the exact campaign scenario: build module A; add new spec module B; A stays fresh.
- Produces (finding 11): default `jaunt check` = contract batteries AND magic freshness; exit 4 if either blocks, naming each stale/unbuilt module with its reason (same strings `status` uses). `--contracts-only` / `--magic-only` scope it (mutually exclusive → argparse error). `--json` gains `"magic": {"fresh": [...], "stale": {...}, "unbuilt": [...]}` mirroring the status payload shape, alongside the existing contract block.
- Constraint: a magic-spec-free project must still exit 0 (baseline-on-unconverted-repo stays clean); a contract-free magic project with drift must exit 4 (the exact no-op scenario from finding 11).

- [ ] Steps: failing tests — repo-map decoupling (sibling-spec-added scenario; repo-map content edit alone → still fresh) then the four check scenarios (fresh both → 0; stale magic only → 4; `--contracts-only` on stale magic → 0; JSON shape) → verify → implement decoupling, then check gating → suite/lint/ty → **commit** in two commits (`fix: repo-map content no longer restales unrelated modules` then `feat: jaunt check gates magic-mode freshness (exit 4 on drift); add --contracts-only/--magic-only`).

### Task 8: Docs sweep (spec Cluster 6b + all doc-facing changes; findings 10, 13 + vocabulary)

**Files:**
- Modify: `CLAUDE.md` (config reference: `[build] emit_stubs`; strict-config note; `@sig` in the three-tier vocabulary; `check` description + CLI table; layout note; `[context]` note that repo-map content no longer affects module staleness), `README.md` if it mentions tiers/layout
- Modify: `src/jaunt/instructions/primer.md` (same vocabulary/check updates — if not already done in Task 3)
- Create: `docs-site/content/docs/configuration.mdx` (annotated reference generated from `init_template.py` content — keep them consistent by embedding the same template text)
- Modify: `docs-site` codex-engine page (fix dangling "Configuration reference" link), quickstart (top-level single-file layout + new `__generated__/<module>.py` mapping + source-roots guidance: package parent vs package dir), adoption docs page (+ coverage recipe with exact snippet below)
- Test: `jaunt tree --check`-style consistency isn't required for docs-site; run any existing docs-site build/lint if configured

Coverage recipe to include verbatim (finding 13):

```toml
[tool.coverage.report]
exclude_lines = ["raise RuntimeError\\(\"spec stub", "pragma: no cover"]
```

with one paragraph: spec stub bodies are unreachable by design (runtime forwards to `__generated__`), so add the stub-raise line to coverage `exclude_lines` before wiring `--cov-fail-under` on a converted module.

- [ ] Steps: write docs; verify every claim against the shipped behavior of Tasks 1–7 (do not document unshipped behavior); include the 1.3.0 migration callout (`jaunt clean && jaunt build` for top-level-module projects; unknown config keys now error). **Commit** (`docs: 1.3.0 — @sig vocabulary, strict config, check gating, layout migration, configuration reference, coverage recipe`).

### Task 9: Integration + green gate

**Files:** whatever fallout requires; no new features.

- [ ] Run `uv run pytest` (full), `uv run ruff check .`, `uv run ruff format .` (CI bot formats anyway — do it locally first), `uv run ty check` — all green.
- [ ] Verify `examples/*/jaunt.toml` all load under strict config; `uv run --project ../.. jaunt status` in one example exits cleanly.
- [ ] Grep the repo for stale references: `timing/__generated__`-style top-level layout in docs/tests, `inner @jaunt.magic` described as the primary sealed marker, `_INIT_TEMPLATE` imports.
- [ ] **Commit** any fixes (`fix: 1.3.0 integration fallout`).

## Self-review notes (spec-coverage check)

- Finding 1/7/8/15 → Task 1. Finding 2/5 → Task 2. Finding 3 → Tasks 3+4. Finding 4/16 → Task 6. Finding 6/9/12 → Task 5 (+docs in 8). Finding 10/13 → Task 8. Finding 11/14 → Task 7. Finding 17 → no action (positive). `@sig` user request → Task 3 (+8).
- Type consistency: `validate_no_import_fallbacks`, `build_stub_source`, `is_jaunt_stub`, `NEEDS_DEP_MARKER`, `context_stats`, `needs_deps`, `emit_stubs` are each defined in exactly one task and consumed by later ones as named.
