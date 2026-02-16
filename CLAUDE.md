# Jaunt — Developer Guide for Coding Agents

Jaunt is a spec-driven code generation framework for Python. Users write
implementation intent as decorator-marked stubs (`@jaunt.magic`) and test intent
as test stubs (`@jaunt.test`). Jaunt generates real implementations and pytest
tests into `__generated__/` directories using an LLM backend (OpenAI or
Anthropic).

## Quick Reference

```bash
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

# Build an example project (requires OPENAI_API_KEY, ANTHROPIC_API_KEY, or CEREBRAS_API_KEY)
cd examples/jwt_auth && uv run --project ../.. jaunt build

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
  external_imports.py  # External import detection
  skills_auto.py    # Auto-generated PyPI skills
  generate/
    base.py              # Abstract GeneratorBackend interface
    openai_backend.py    # OpenAI provider
    anthropic_backend.py # Anthropic/Claude provider
    cerebras_backend.py  # Cerebras provider
  prompts/          # LLM prompt templates (Jinja-like {{var}})
tests/              # pytest test suite (~41 files)
examples/           # Runnable example projects
```

## Key Concepts

- **Spec**: A decorated Python function/class stub that describes *what* to
  implement. Uses `@jaunt.magic` for implementations, `@jaunt.test` for tests.
- **Generated dir**: Output directory (default `__generated__/`) where LLM-
  generated code is written. Configurable via `jaunt.toml` or
  `JAUNT_GENERATED_DIR` env var.
- **Incremental builds**: Jaunt computes SHA-256 digests over spec source +
  decorator kwargs + transitive deps. Only stale modules are regenerated.
- **Dependency graph**: Built from explicit `deps=` kwargs and optional
  AST-based inference. Topologically sorted; cycle detection with clear errors.

## Configuration (`jaunt.toml`)

```toml
version = 1

[llm]
provider = "openai"          # or "anthropic", "cerebras"
model = "gpt-5.2"            # or "claude-sonnet-4-20250514", "llama-4-scout-17b-16e-instruct", etc.
api_key_env = "OPENAI_API_KEY"  # or "ANTHROPIC_API_KEY", "CEREBRAS_API_KEY"

[paths]
source_roots = ["src", "."]
test_roots = ["tests"]
generated_dir = "__generated__"

[build]
jobs = 8
infer_deps = true

[test]
jobs = 4
infer_deps = true
pytest_args = ["-q"]

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

jaunt status                  # Show stale vs fresh modules
jaunt status --json           # Machine-readable status

jaunt watch                   # Auto-rebuild on file changes
jaunt watch --test            # Build + test on change
```

Common flags: `--root`, `--config`, `--jobs N`, `--force`, `--target`,
`--no-infer-deps`, `--no-progress`, `--json`.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 2    | Config, discovery, or dependency cycle error |
| 3    | Code generation error |
| 4    | Pytest failure |

## Testing Changes

Always run the full test suite after changes:

```bash
uv run pytest
```

The test suite uses mocking for OpenAI calls and does not require API keys.
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
