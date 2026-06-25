# Jaunt — Developer Guide for Coding Agents

Jaunt is a spec-driven code generation framework for Python. Users write
implementation intent as decorator-marked stubs (`@jaunt.magic`) and test intent
as test stubs (`@jaunt.test`). Jaunt generates real implementations and pytest
tests into `__generated__/` directories using the OpenAI **Codex** CLI as its
code-generation engine (`codex exec`).

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

# Optional extras:
# - pytest markers for async tests
pip install jaunt[async]     # for pytest-asyncio marker
pip install jaunt[anyio]     # for anyio marker

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
  while `check` is the deterministic CI gate (no API key).
- **Generated dir**: Output directory (default `__generated__/`) where LLM-
  generated code is written. Configurable via `jaunt.toml` or
  `JAUNT_GENERATED_DIR` env var.
- **Incremental builds**: Jaunt computes SHA-256 digests over spec source +
  decorator kwargs + transitive deps, and separately tracks each module's
  exported dependency API. Signature changes, full docstring contract edits,
  and whole-class member/method changes can make dependents stale too.
- **Whole-class `@magic`**: A class-level `@jaunt.magic` can be docstring-only
  (Jaunt designs the API), stubs-only (Jaunt implements declared methods), or a
  mix of stubs and preserved members. Use `@jaunt.preserve` on a method to keep
  it hand-written even if its body looks like a stub.
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

[test]
jobs = 4
infer_deps = true
pytest_args = ["-q"]

[contract]
battery_dir = "tests/contract"     # where derived contract batteries are written
derive = ["examples", "errors"]    # case kinds derived from docstring prose
strength = true                    # run mutation-based strength scoring at reconcile

[prompts]
# Optional file path overrides for LLM prompt templates.
# Leave empty to use the packaged defaults in src/jaunt/prompts/.
build_system = ""
build_module = ""
test_system = ""
test_module = ""
```

## CLI Commands

```bash
jaunt build                   # Generate implementations for @jaunt.magic specs
jaunt build --force           # Force full regeneration
jaunt build --target my_app.specs  # Build specific module only

jaunt test                    # Generate tests and run pytest
jaunt test --no-build         # Skip build step
jaunt test --no-run           # Generate tests without running pytest

jaunt init                    # Scaffold jaunt.toml + src/ + tests/
jaunt init --force            # Overwrite existing jaunt.toml

jaunt clean                   # Remove all __generated__ directories
jaunt clean --dry-run         # Show what would be removed

jaunt status                  # Show which modules are stale, including upstream API fallout
jaunt status --json           # Machine-readable status

jaunt adopt <module:func>     # Add @jaunt.contract to existing code and derive its battery
jaunt reconcile               # Derive/refresh committed contract batteries (calls the model)
jaunt check                   # Verify committed batteries deterministically (CI gate, no model)
jaunt eject <module:func>     # Remove contract tracking; leave plain Python + plain pytest

jaunt watch                   # Auto-rebuild on file changes
jaunt watch --test            # Build + test on change
```

Common flags: `--root`, `--config`, `--jobs N`, `--force`, `--target`,
`--no-infer-deps`, `--no-progress`, `--json`.

Note: `jaunt check` returns exit code `4` on any blocking drift state (unbuilt /
stale-prose / signature-drift / behavior-drift).

## Exit Codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 2    | Config, discovery, or dependency cycle error |
| 3    | Code generation error |
| 4    | Pytest failure, or contract `check`/`reconcile` block (stale prose, signature/behavior drift) |

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
