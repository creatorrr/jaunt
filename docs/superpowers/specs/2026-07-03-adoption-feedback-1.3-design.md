# Jaunt 1.3.0 — Adoption-Feedback Improvements (Design)

Date: 2026-07-03
Status: Approved
Source: `FEEDBACK.md` (mem-mcp-b adoption campaign, 2026-07-03), findings 1–10
verified against source by three independent read passes; addendum findings
11–14 folded in same day (14 is positive-only, no action).

## Decisions (user-confirmed)

- One comprehensive **1.3.0** release, single PR.
- **Strict by default**: unknown config keys error (exit 2); generated-code
  violations are hard build failures (they feed the regeneration retry loop);
  the top-level generated-layout mapping is fixed with a clean-rebuild
  migration note.
- Typing: fix `runtime.py` overloads **and** emit `.pyi` stubs from generated
  implementations.
- Cost finding: **instrument only** — per-block context accounting, no
  trimming this release.
- New `@jaunt.sig` decorator becomes the canonical sealed-method marker;
  inner `@jaunt.magic` stays as a quiet back-compat alias.

## Cluster 1 — Generated-code guardrails (findings 1, 7, 8)

### Prompt changes

`src/jaunt/prompts/build_module.md` (with a one-line reinforcement in
`codex_preamble.md`):

1. **No fallback implementations.** Never wrap imports in `try/except` to
   provide fallbacks; import failures must raise. Never define a second,
   divergent implementation of a contract symbol.
2. **Define, don't re-import, your own spec symbols.** Clarify the reuse rule
   ("Reuse handwritten symbols from `{{spec_module}}`…", currently line 62):
   the generated module must define every spec symbol itself and must never
   import any of them back from the spec module.
3. **Loud marker for undeclared deps.** If the contract implies behavior from
   a module not listed in Dependency APIs, do not invent an import. Inline the
   minimal logic and mark the site:
   `# JAUNT-NEEDS-DEP: <module>:<name> — <one-line reason>`.

### Validation changes (`src/jaunt/validation.py`, hard errors)

- **New check — no import fallbacks:** walk the generated AST for `ast.Try`
  where any handler catches `ImportError`/`ModuleNotFoundError` and the try
  body contains an import of the spec module, a declared dependency module, or
  any first-party module. Scoped deliberately: optional *third-party* import
  guards remain legal (a spec may legitimately say "use ujson if available").
- **New check — no spec self-import:** beside `_validate_build_contract_only`
  (which already receives `spec_module` and `expected_names`), flag
  `from <spec_module> import X` where `X` is in the module's own
  `expected_names`.

Both are build errors, so they flow into the existing retry-with-diagnostics
path.

### Builder change

Scan generated source for `JAUNT-NEEDS-DEP` markers; surface as build
warnings (per module, with the marker text) in the human report and under a
`needs_deps` key in `--json`.

## Cluster 2 — Config strictness + schema discoverability (findings 2, 5)

- New helper in `src/jaunt/config.py`:
  `_reject_unknown(tbl, allowed, where)` — applied to the top-level table
  (allowed = known section names) and to every known section's keys
  (allowlists include retained back-compat keys such as
  `skills.max_chars_per_skill`, `skills.inject_user_skills`, and the whole
  informational `[llm]` key set, plus nested `context.search`).
- Unknown section/key → `JauntConfigError` (exit 2) with a
  `difflib.get_close_matches` suggestion: `unknown section [gate] — did you
  mean [semantic_gate]?`.
- Move `_INIT_TEMPLATE` and `_INIT_SPEC_TEMPLATE` from `cli.py` to a new
  shared `src/jaunt/init_template.py`. `jaunt init` imports from there.
  `jaunt instructions` in the no-project branch
  (`instructions/__init__.py::_project_block`) appends the full annotated
  `jaunt.toml` template so the schema is visible exactly when a user needs to
  write the file.
- Guard test: every section/key present in `_INIT_TEMPLATE` (and in the
  CLAUDE.md reference config) loads without an unknown-key error, so the
  template and the allowlists cannot drift apart. A second test asserts a
  typo'd section (`[gate]`) and a typo'd key (`reasoning-effort`) each raise.

## Cluster 3 — `@sig` + decorator typing (finding 3, part 1)

### `@jaunt.sig`

- New decorator in `src/jaunt/runtime.py`, exported from `jaunt/__init__.py`.
  Canonical marker for **sealed** methods inside a whole-class `@jaunt.magic`
  spec — same registry semantics as today's inner bare `@jaunt.magic`.
- Accepts both `@sig` and `@sig()` (no kwargs either way), eliminating the
  bare-vs-call confusion that motivated it. Kwargs → error.
- Inner `@jaunt.magic` remains a supported, silent back-compat alias.
- `@sig` anywhere outside a whole-class spec (e.g. on a top-level function) is
  a clear discovery/build error pointing the user to `@jaunt.magic`.
- All docs, prompt templates, `instructions/primer.md`, and the three-tier
  vocabulary (preserve / **sig** / guidepost) switch to `@sig` as the primary
  name; `@jaunt.preserve` and class-level `@jaunt.magic` are unchanged.

### Overload/typing fixes (`runtime.py`)

- Fix the `magic` overload set: bare `@magic` → `F`; `@magic(**kwargs)` →
  `Callable[[F], F]`. The current first overload
  `def magic() -> Callable[[F], Any]` (runtime.py:173-174) erases decorated
  symbols to `Any` — remove/reorder it so `@magic()` also preserves `F`.
- Add an `if TYPE_CHECKING:` identity branch so `magic`, `sig`, `test`, and
  `contract` are signature-invisible to Pyright/ty.

## Cluster 4 — `.pyi` emission (finding 3, part 2)

- After a successful module build, emit `<spec_module>.pyi` as a sibling of
  the spec module. Content:
  - handwritten symbols: signatures/class shapes from the **spec module's**
    AST (annotations preserved);
  - spec symbols: real signatures from the **generated implementation** — so
    docstring-only classes expose their designed `__init__`/methods to type
    checkers. (A `.pyi` fully replaces the module for type checkers, which is
    also what erases decorator noise at call sites for built modules.)
- Provenance header comment (same digest/fingerprint scheme as generated
  files). The stub participates in freshness: `jaunt status`/`jaunt check`
  flag a missing or stale `.pyi`; `jaunt clean` removes only header-marked
  stubs; a pre-existing hand-authored `.pyi` is never overwritten (skip +
  warning).
- Config: `[build] emit_stubs = true` (opt-out). Stubs are committed, same
  convention as `__generated__` output.

## Cluster 5 — Module identity + layout (findings 6, 9)

- `src/jaunt/paths.py`: a **top-level** spec module `timing` now maps to
  generated module `__generated__.timing` → file `__generated__/timing.py`
  (today: `timing.__generated__` → `timing/__generated__/__init__.py`, a
  directory sibling that shadows the module name). Package members are
  unchanged (`pkg.mod` → `pkg/__generated__/mod.py`). Both the runtime loader
  and the builder resolve through the same two helpers, so this is one
  coordinated change with **no dual-path fallback**.
- Migration: release-notes callout — `jaunt clean && jaunt build`
  (`clean` discovers old-form `<module>/__generated__/` dirs by scan already).
- Discovery (`src/jaunt/discovery.py`): warn once per run when a derived
  top-level module name shadows `sys.stdlib_module_names` or an installed
  distribution (via `importlib.metadata.packages_distributions()`; no
  `find_spec`, to avoid import side effects).
- Root-choice doctor check (finding 12): when a configured source root is
  itself a package directory (contains `__init__.py`), warn at config
  load/`jaunt init`/first discovery that module names will be bare and the
  root should usually be the package *parent* (e.g. `src`). Warning, not
  error — bare-module layouts are legitimate; the wrong-root failure mode
  currently only manifests at first build.
- Docs: guidance on choosing `source_roots` (package parent vs package dir —
  root placement determines the dotted module identity and therefore import
  paths in generated code), plus quickstart coverage of the top-level
  single-file case.

## Cluster 6 — Cost instrumentation + docs (findings 4, 10)

- Builder: per-block size accounting — chars and estimated tokens (chars/4)
  for preamble, module contract, deps block, package context, repo map, and
  blueprint source — per module, shown in the build summary and under
  `context_stats` in `--json`. No trimming this release; this produces the
  data for a 1.4 decision.
- Docs-site: add the missing configuration reference page (sourced from the
  same annotated template as `init`/`instructions`), fix the codex-engine
  page's dangling "Configuration reference" link. The `creatorrr.github.io`
  deep-link redirect drop is a jaunt.ing hosting-config note, not code.
- Coverage recipe (finding 13): one adoption-docs paragraph — spec stub
  bodies are unreachable by design (runtime forwards to `__generated__`), so
  coverage gates need the stub-raise line added to coverage `exclude_lines`;
  include the exact snippet.

## Cluster 7 — `jaunt check` gates magic freshness (finding 11, HIGH)

Today `check` verifies `@jaunt.contract` batteries only; on a magic-only
project it prints "0 contract function(s)" and exits 0 regardless of
spec↔generated drift — while all adopter-facing framing sells `check` as
*the* deterministic CI gate. Fix, per strict-by-default:

- `jaunt check` additionally runs the deterministic magic-mode freshness
  computation (same Layer-A detection `jaunt status` uses — digests only, no
  model call, no API key) and exits 4 when any module is unbuilt or stale,
  naming the modules and the reason (spec drift / upstream API fallout /
  missing or stale `.pyi` per Cluster 4).
- Scoping flags for CI granularity: `--contracts-only` and `--magic-only`
  (default = both). `--json` gains a `magic` block mirroring the `status`
  payload.
- Docs: CI-gating guidance updated — `jaunt check` is the single required CI
  command for both modes; the exit-code table stays as-is (4 = blocking
  drift).

## Testing & release

- Unit tests per cluster: validation checks (fallback ladder, self-import,
  third-party guard stays legal), config unknown-key rejection + template
  round-trip, `@sig` registry semantics + misuse errors, overload behavior
  (typing test via ty/pyright snippet or `assert_type`), paths mapping (old →
  new), stub emission/skip/clean/check integration, `needs_deps` and
  `context_stats` in `--json`, `check` magic-freshness gating (fresh → 0,
  stale/unbuilt → 4, scoping flags, JSON payload), package-root doctor
  warning.
- Full `uv run pytest`, `uv run ruff check`, `uv run ty check` green.
- Single PR → merge publishes **1.3.0** (automated changelog/tag/PyPI).
- Implementation: multi-wave dynamic Workflow (opus@high driving Codex CLI at
  effort=medium) in a dedicated impl worktree; clusters 1/2/3 are
  parallel-safe; 4 depends on 3; 7 depends on 4 (stub staleness feeds
  `check`); 5/6 independent; docs last. End-of-branch review with codex@high;
  fix findings before merge.
