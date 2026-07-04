# Jaunt — Developer Guide for Coding Agents

Jaunt is a spec-driven code generation framework for Python. Users write
implementation intent as decorator-marked stubs (`@jaunt.magic`) and test intent
as test stubs (`@jaunt.test`). Jaunt generates real implementations and pytest
tests into `__generated__/` directories using the OpenAI **Codex** CLI as its
code-generation engine (`codex exec`).

> **Codex model policy:** strictly use `gpt-5.5` for EVERY Codex invocation
> (`codex exec`, `codex mcp-server`, `codex app-server`) — never `gpt-5.2`,
> `gpt-5.2-codex`, or any other variant. The only deliberate exception is the
> small `[semantic_gate]` judge model (`gpt-5.4-mini`).

## Quick Reference

```bash
# Requires the `codex` CLI installed and authenticated (`codex login`).

# Install
uv sync --frozen

# Enable git pre-commit hooks (ruff lint+format, ty type check)
git config core.hooksPath .githooks

# Run tests (unit tests for jaunt itself)
uv run pytest

# Lint
uv run ruff check .

# Typecheck
uv run ty check

# Build an example project (requires the `codex` CLI, authenticated via `codex login`)
cd examples/jwt_auth && uv run --project ../.. jaunt build

# Batteries included: pytest, pytest-asyncio, anyio, rich, and watchfiles
# ship in the base install — no optional extras.

# Run with JSON output (for programmatic consumption)
jaunt build --json
jaunt test --json
```

## Project Layout

```
src/jaunt/          # Library source
  cli.py            # CLI entry point (build, test, init, clean, status, watch)
  runtime.py        # @magic and @test decorators
  builder.py        # Build orchestration and parallel scheduling
  tester.py         # Test generation and pytest runner
  config.py         # jaunt.toml parsing
  deps.py           # Dependency graph (explicit + AST-inferred)
  discovery.py      # Module scanning
  registry.py       # Global spec registries
  digest.py         # SHA-256 digests for incremental builds
  validation.py     # AST validation of generated code
  diagnostics.py    # Error formatting and actionable hints
  watcher.py        # File watching for `jaunt watch`
  parse_cache.py    # Persistent AST parse cache
  paths.py          # Path resolution helpers
  header.py         # Generated file header format
  module_api.py     # Exported API summaries/digests for dependency-aware rebuilds
  external_imports.py  # External import detection
  skills_auto.py    # Auto-generated PyPI skills
  codex_executor.py   # Codex-driven agent executor (auto-skills)
  generate/
    base.py              # Abstract GeneratorBackend interface
    codex_backend.py    # Codex engine (drives `codex exec`)
  repo_context/     # Maintained treedocs.yaml repo map + colgrep retrieval
    tree.py              # TreeDoc model, incremental sync, atomic write, drift
    describe.py          # AST baseline (+ optional LLM enrichment) descriptions
    digests.py           # Source-content digests + .jaunt/tree-cache.json sidecar
    block.py             # Render repo-map prompt block + annotate package tree
    search.py            # colgrep wrapper (detect/index/query, graceful fallback)
    api.py               # High-level sync_tree / repo_map_block_for_build / check_drift
  prompts/          # LLM prompt templates (Jinja-like {{var}})
tests/              # pytest test suite (~41 files)
examples/           # Runnable example projects
```

## Key Concepts

- **Spec**: A decorated Python function/class stub that describes *what* to
  implement. Uses `@jaunt.magic` for implementations, `@jaunt.test` for tests.
  The full cleaned docstring is part of the behavioral contract.
- **Contract mode**: Committed code is canonical; `@jaunt.contract` is a runtime
  no-op marker; the docstring is the contract. Jaunt derives a committed pytest
  battery in `tests/contract/`. `reconcile` is the only model-calling command,
  while `check` is the deterministic CI gate (no API key). Covers top-level
  functions (sync or async) and whole classes; derived cases may declare pytest
  fixtures (`Fixtures: db`) resolved from `tests/contract/conftest.py`.
- **Generated dir**: Output directory (default `__generated__/`) where LLM-
  generated code is written. Configurable via `jaunt.toml` or
  `JAUNT_GENERATED_DIR` env var.
- **Incremental builds**: Jaunt computes SHA-256 digests over spec source +
  decorator kwargs + transitive deps, and separately tracks each module's
  exported dependency API. Signature changes, full docstring contract edits,
  and whole-class member/method changes can make dependents stale too.
- **Whole-class `@magic`**: A class-level `@jaunt.magic` can be docstring-only
  (Jaunt designs the API), stubs-only (Jaunt implements declared methods), or a
  mix. Each method sits in exactly one of three tiers: **preserved**
  (`@jaunt.preserve` — hand-written, emitted verbatim even if the body looks like
  a stub), **sealed** (`@jaunt.sig` on a stub; inner `@jaunt.magic` is a supported
  alias — Jaunt writes the body but
  the declared signature is enforced *exactly*: params, defaults, annotations, and
  return type may not drift, and drift is a hard build error), and **guidepost**
  (an unmarked stub — the model may adapt the signature, rename/add params, or
  split it into several methods as long as the documented behavior is delivered;
  drift is warn-only). `@jaunt.sig` (and the inner `@jaunt.magic` alias) takes no
  kwargs and can't sit under `@property` (v1). A spec'd base class named in the class
  header is an **always-on dependency edge** (never gated by `infer_deps` —
  inheritance is a structural fact), and a cross-module base's *generated public
  API* (signatures and docstrings) participates in the subclass's staleness:
  change the base's API and the subclass restales; a body-only rebuild of the base
  does not.
- **Auto-generated class tests**: Class specs can get generated pytest coverage
  through explicit `@jaunt.test(targets=Cls)` test specs, opt-in implicit
  `@jaunt.magic(test=True)`, or the `[test] auto_class_tests` config flag.
- **Dependency graph**: Built from explicit `deps=` kwargs and optional
  AST-based inference. Topologically sorted; cycle detection with clear errors.

## Configuration (`jaunt.toml`)

```toml
version = 1

[agent]
engine = "codex"          # the only supported engine

[codex]
model = "gpt-5.5"
reasoning_effort = "high"  # low | medium | high
sandbox = "workspace-write"

# [llm] is retained but informational under Codex: Codex authenticates via
# `codex login` / CODEX_API_KEY, not `llm.api_key_env`.
[llm]
provider = "openai"
model = "gpt-5.2"
api_key_env = "OPENAI_API_KEY"

[paths]
source_roots = ["src", "."]
test_roots = ["tests"]
generated_dir = "__generated__"

[build]
jobs = 8
infer_deps = true
async_runner = "asyncio"
emit_stubs = true         # emit provenance-headed .pyi stubs next to each spec module (opt-out)

[test]
jobs = 4
infer_deps = true
pytest_args = ["-q"]

[daemon]
poll_interval = 2.0         # seconds between HEAD polls
max_jobs = 0                # 0 -> build.jobs
notify_command = ""         # optional shell command run on job completion
auto_commit = false         # default: park green jobs as proposals (land with `jaunt jobs land`);
                            #   true restores auto-commit-on-green (pre-1.2.0 behavior)

[skills]
auto = true                 # auto-generate PyPI helper skills for imported libs
builtin = true              # seed Jaunt's bundled builtin skills into the Codex workspace
builtin_skills = [          # the default set (override to trim/extend)
  "asyncpg", "dbos", "descope", "fastmcp", "openai", "pydantic", "pydantic-ai",
  "pytest", "ruff", "spacy", "starlette", "ty", "uv",
]

[context]
repo_map = true             # maintain treedocs.yaml + inject a repo map into build prompts
repo_map_file = "treedocs.yaml"
enrich = false              # opt-in: LLM-enrich descriptions (else AST-only, offline)
max_chars = 6000            # cap the injected repo-map block
overview = false            # opt-in: model-written architecture overview injected into build
                            #   prompts, digest-cached to .jaunt/PROJECT_OVERVIEW.md. Jaunt
                            #   calls the model once when the spec sources, repo map, injected
                            #   project docs (README/AGENTS/CLAUDE), or overview prompt templates
                            #   change; subsequent builds reuse the cached prose. The overview
                            #   model call is charged against [llm] max_cost_per_build and shown
                            #   in the cost summary. Toggling this flag participates in build
                            #   freshness, so enabling it triggers a one-time rebuild of already-
                            #   built modules (it does not affect the test-kind fingerprint).
                            #   Off by default — enable when you want the LLM to receive a prose
                            #   summary of the whole codebase alongside the per-spec context.

[context.search]            # colgrep (LightOn next-plaid) semantic retrieval
enabled = false             # opt-in; requires the `colgrep` binary on PATH
internal_retrieval = true   # Jaunt queries `colgrep --json` and seeds _context/relevant_*.py
max_hits = 8

[contract]
battery_dir = "tests/contract"     # where derived contract batteries are written
derive = ["examples", "errors"]    # case kinds derived from docstring prose
strength = true                    # run mutation-based strength scoring at reconcile

[semantic_gate]
enabled = true              # gate behaviorally-equivalent edits before a gpt-5.5 rebuild
model = "gpt-5.4-mini"      # small model that judges contract equivalence (must work via codex exec)
reasoning_effort = "high"   # low | medium | high

[prompts]
# Optional file path overrides for LLM prompt templates.
# Leave empty to use the packaged defaults in src/jaunt/prompts/.
build_preamble = ""         # override for the Jaunt preamble (codex_preamble.md)
build_system = ""
build_module = ""
test_system = ""
test_module = ""
project_overview_system = ""  # override for the overview system prompt
project_overview_user = ""    # override for the overview user prompt ({{project_docs}}, {{repo_map}})
```

**Strict config.** Unknown `jaunt.toml` sections and keys are rejected with a
`JauntConfigError` (exit 2) and a "did you mean …" suggestion — a typo'd section
like `[gate]` or key like `reasoning-effort` fails loudly instead of being
silently ignored. `jaunt instructions` (run before a `jaunt.toml` exists) prints
the full annotated config schema.

Every build prompt opens with a static **Jaunt preamble** (`src/jaunt/prompts/codex_preamble.md`)
that frames what Jaunt is and states the signature/docstring contract. It is always-on and
adds no model call. Its content is part of the build freshness fingerprint (like
`build_system`/`build_module`), so editing the preamble — or pointing at a different one —
regenerates already-built modules. Replace it project-wide via
`[prompts] build_preamble = "path/to/my_preamble.md"` (a relative path resolves against the
project root).

Repo-map *content* no longer participates in per-module staleness (1.3.0):
adding or editing a sibling spec's repo-map entry never restales an unchanged
module, and `jaunt build`/`jaunt check` ignore repo-map drift (`jaunt status` may
still note it informationally). The `[context] repo_map` on/off toggle remains a
fingerprint input.

Skills are no longer injected as prompt text; Codex discovers them natively from a
seeded `.agents/skills/` workspace. `max_chars_per_skill` and `inject_user_skills` are
retained for back-compat but unused by the Codex builder.

**Change detection (two layers).** Spec freshness is computed from an AST-normalized
contract digest, so ruff reformatting, comment edits, and whitespace/quote changes no
longer mark a module stale (Layer A — deterministic, build + test). When a spec's
docstring genuinely changes but its signature/structure do not, a small
`[semantic_gate]` model judges whether the change is behaviorally meaningful: if it is
equivalent, Jaunt **re-freezes** the module (rewrites the header digests over the
validated, unchanged generated body) instead of paying for a full `gpt-5.5` rebuild
(Layer B). Structural changes, validation failures, and any gate error fail safe to a
rebuild. `--json` reports re-frozen modules under `"refrozen"`. The gate model must be
runnable via `codex exec` (e.g. `gpt-5.4-mini`); `gpt-5.4-nano` is not — `codex exec`
attaches a `tool_search` tool nano rejects.

## CLI Commands

```bash
jaunt build                   # Generate implementations for @jaunt.magic specs
jaunt build --force           # Force full regeneration
jaunt build --target my_app.specs  # Build specific module only
jaunt build --no-auto-skills  # Disable auto-skill injection into build prompts
jaunt build --no-semantic-gate  # Skip the Layer B gate; rebuild on any real change (Layer A still applies)

jaunt test                    # Generate tests and run pytest
jaunt test --no-build         # Skip build step
jaunt test --no-run           # Generate tests without running pytest
jaunt test --no-redact-derived  # Debug: expose full failure detail (defeats the held-out implementer/tester barrier)

jaunt init                    # Scaffold jaunt.toml + src/ + tests/
jaunt init --force            # Overwrite existing jaunt.toml

jaunt clean                   # Remove all __generated__ directories
jaunt clean --dry-run         # Show what would be removed

jaunt status                  # Show which modules are stale, including upstream API fallout
jaunt status --json           # Machine-readable status

jaunt specs                   # List @jaunt.magic specs and their dependency graph
jaunt specs --json            # Machine-readable spec list (for agents/tooling)

jaunt instructions            # Print a project-aware agent primer (load into an agent's context)
jaunt instructions --json     # {command, ok, text, project} for tooling/MCP

jaunt tree                    # Maintain treedocs.yaml (1-line descriptions of dirs + .py files)
jaunt tree --check            # CI gate: exit 4 if the tree is stale (new/ghost paths or edited files)
jaunt tree --enrich           # Force LLM enrichment of descriptions this run
jaunt build --no-repo-map     # Disable repo-map injection for one build

jaunt adopt <module:func>     # Add @jaunt.contract to existing code and derive its battery
jaunt reconcile               # Derive/refresh committed contract batteries (calls the model)
jaunt check                   # CI gate (no model): verifies contract batteries AND magic-mode freshness; exit 4 on drift
jaunt check --contracts-only  # or --magic-only: scope the gate to one mode (mutually exclusive)
jaunt eject <module:func>     # Remove contract tracking; leave plain Python + plain pytest

jaunt daemon start            # Background codegen: commit-triggered isolated jobs; parks green jobs as proposals by default ([daemon] auto_commit = false)
jaunt daemon stop|status      # Stop / inspect the daemon (status shows landing mode: propose-only | auto-commit)
jaunt jobs                    # Job records + would-rebuild preview; show <id> [--full]; retry <id>
jaunt jobs land <id>|--all    # Land parked proposal(s) as provenance commits (re-validates; no --force); --all in job-creation order
jaunt jobs discard <id>       # Discard a parked proposal (marks DISCARDED, removes patch)
jaunt jobs wait               # Block until daemon jobs finish (0 green incl. PROPOSED, 4 failed/parked, 5 timeout)
jaunt log                     # Tail the JAUNT_LOG change journal (-n N, --module X)
jaunt guard                   # PreToolUse hook: warn when agents touch __generated__ (see docs/hooks.md)

jaunt watch                   # Auto-rebuild on file changes
jaunt watch --test            # Build + test on change
```

Common flags: `--root`, `--config`, `--jobs N`, `--force`, `--target`,
`--no-infer-deps`, `--progress {auto,rich,plain,none}`, `--no-progress`, `--json`.

Note: `jaunt check` returns exit code `4` on any blocking drift state —
contract drift (unbuilt / stale-prose / signature-drift / behavior-drift) or
magic-mode drift (any unbuilt or stale `@jaunt.magic` module, including a
missing/stale `.pyi` stub). A project with no magic specs and no contract drift
exits 0.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 2    | Config, discovery, or dependency cycle error |
| 3    | Code generation error |
| 4    | Pytest failure, contract `check`/`reconcile` block, or daemon job failed/parked while waiting |
| 5    | Timeout while waiting for daemon jobs |

## Testing Changes

Always run the full test suite after changes:

```bash
uv run pytest
```

The test suite mocks the generator backend and does not require API keys.
Tests are organized by module — `test_cli.py`, `test_builder_io.py`,
`test_config.py`, etc.

## Lint and Format

```bash
uv run ruff check --fix .
uv run ruff format .
```

Ruff is configured for line-length 100, Python 3.12+, with rules E/F/I/UP/B.

## JSON Output Mode

Use `--json` flag with any command (`build`, `test`, `init`, `clean`, `status`,
`watch`) for machine-readable output on stdout. Errors still go to stderr.
Progress bars are suppressed in JSON mode.

```bash
jaunt build --json
# {"command": "build", "ok": true, "generated": ["mymod"], "skipped": [], "failed": {}}
```
