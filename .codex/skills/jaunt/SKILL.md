---
name: jaunt
description: "Use when working with the Jaunt spec-driven code generation framework for Python. Trigger for requests mentioning Jaunt, @jaunt.magic, @jaunt.test, spec stubs, jaunt build, jaunt test, jaunt.toml, __generated__ directories, or writing specs/tests that Jaunt will generate implementations for. Also use when the user wants to set up a new Jaunt project, configure LLM providers, debug build failures, or understand the spec-driven development workflow."
---

# Jaunt (spec-driven code generation)

## Overview

Jaunt is a Python framework where humans write **intent** as decorator-marked stubs and Jaunt generates **implementations** via an LLM backend. The core loop: write specs, write tests, run `jaunt build`, review generated code, iterate.

Jaunt has two internal runtimes:
- `legacy`: Jaunt talks to the provider SDK directly.
- `aider`: Jaunt runs generation tasks through Aider, while Jaunt still owns discovery, validation, retries, freshness, and final writes.

Your role as an AI assistant: help author and refine spec stubs and test specs. **Do not** hand-write implementations for `@jaunt.magic` symbols unless the user explicitly asks to bypass Jaunt.

## Repo Triage (Do First)

1. Check for `jaunt.toml` at the project root. If missing, the project needs `jaunt init`.
2. Identify `[paths]` in `jaunt.toml`: `source_roots` (where specs live), `test_roots` (where test specs live), `generated_dir` (output directory name, usually `__generated__`).
3. Identify the LLM provider: `[llm].provider` is `"openai"`, `"anthropic"`, or `"cerebras"`. The API key env var is in `[llm].api_key_env`.
4. Identify the internal runtime: `[agent].engine` is `"legacy"` or `"aider"`. If it is `"aider"`, also inspect `[aider]`.
5. Check for existing `__generated__/` directories to see what has already been built.
6. Run `jaunt status` to inspect stale vs fresh modules. Upstream API changes should make dependents stale too.

## Core Workflow

### 1) Write Spec Stubs (`@jaunt.magic`)

Spec stubs define **what** to implement. The LLM generates the **how**.

```python
import jaunt

@jaunt.magic()
def slugify(title: str) -> str:
    """
    Convert a title to a URL-safe slug.

    Rules:
    - Lowercase the input.
    - Replace whitespace runs with a single "-".
    - Remove non-ASCII-alphanumeric characters except "-" and "_".
    - Raise ValueError if the result is empty.
    """
    raise RuntimeError("spec stub (generated at build time)")
```

**Principles for good specs:**
- Be explicit about behavior: inputs, outputs, invariants, what "correct" means.
- Specify failures: name the exception type and condition.
- Define edge cases: empty inputs, `None`, boundary values, duplicates.
- Constrain the solution when it matters: complexity, determinism, ordering.
- Prefer pure logic: move I/O behind parameters (dependency injection).
- Use full type annotations on all parameters and return types.
- The full docstring is the contract; later lines matter just as much as the summary line.
- For whole-class specs, the exported API includes declared members and method signatures, not just the class name.

**Spec patterns:**

Pure function:
```python
@jaunt.magic()
def normalize_email(raw: str) -> str:
    """Normalize an email. Strip whitespace, lowercase. Raise ValueError if invalid."""
    raise RuntimeError("spec stub (generated at build time)")
```

Function with dependencies (use `deps=` to declare):
```python
@jaunt.magic(deps=[normalize_email])
def is_corporate_email(raw: str, *, domain: str = "example.com") -> bool:
    """Return True iff normalize_email(raw) belongs to domain."""
    raise RuntimeError("spec stub (generated at build time)")
```

Stateful class:
```python
@jaunt.magic()
@dataclass
class LRUCache:
    """Fixed-capacity LRU cache. O(1) get/set/size."""
    capacity: int
    def get(self, key: str) -> object | None: ...
    def set(self, key: str, value: object) -> None: ...
    def size(self) -> int: ...
```

For whole-class specs like this, changing `get`, `set`, `size`, or declared class attributes is a dependency-API change for downstream modules.

Async function:
```python
@jaunt.magic()
async def retry(op: Callable[[], Awaitable[object]], *, attempts: int, base_delay_s: float) -> object:
    """Retry op() with exponential backoff. Re-raise last exception if all fail."""
    raise RuntimeError("spec stub (generated at build time)")
```

### 2) Write Test Specs (`@jaunt.test`)

Test specs describe the test intent. Jaunt generates runnable pytest tests.

```python
@jaunt.test()
def test_slugify_basic() -> None:
    """
    Assert slugify:
    - "Hello World" -> "hello-world"
    - "  A  B  " -> "a-b"
    """
    raise AssertionError("spec stub (generated at test time)")

@jaunt.test()
def test_slugify_rejects_empty() -> None:
    """slugify("!!!") raises ValueError (nothing remains after filtering)."""
    raise AssertionError("spec stub (generated at test time)")
```

**Principles for good test specs:**
- Deterministic: no network, no clock unless injected.
- Small and focused: one behavioral assertion per test when practical.
- Black-box behavior: test the contract, not implementation details.
- Include negative tests: errors and invalid input paths.
- Name must start with `test_`.
- In Aider mode, Jaunt may generate 1-2 obvious contract-adjacent extra cases. Still spell out every required behavior explicitly in the spec.

### 3) Build and Test

```bash
# Generate implementations
jaunt build

# Generate tests and run pytest
jaunt test

# Force full regeneration (ignore incremental cache)
jaunt build --force

# Build specific module only
jaunt build --target my_app.specs

# Generate tests without running them
jaunt test --no-run

# Elaborate a checked-in skill scaffold
jaunt skill build rich

# Refresh Jaunt-managed auto-generated skills
jaunt skill refresh

# See stale vs fresh modules
jaunt status
```

### 4) Review and Iterate

- Review generated code in `__generated__/` directories.
- Never edit `__generated__/` files by hand (they will be overwritten).
- If output is wrong, refine the spec stub or test spec and regenerate.
- Use `prompt=` decorator kwarg for extra LLM instructions on a specific spec.

## Aider Runtime Notes

- Install the runtime with `pip install jaunt[aider]` or `uv sync --extra aider`.
- Enable it with:

```toml
[agent]
engine = "aider"

[aider]
build_mode = "architect"
test_mode = "code"
skill_mode = "code"
editor_model = ""
map_tokens = 0
save_traces = false
```

- The CLI commands stay the same in Aider mode: `jaunt build`, `jaunt test`, `jaunt skill build`, `jaunt skill refresh`, and `jaunt watch`.
- `jaunt watch` uses the configured runtime too. Watch cycles stay sequential, but the build/test work inside a cycle can still use normal Jaunt parallelism.
- For best parallelism in Aider mode, keep `[llm].api_key_env` on the provider's canonical name (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CEREBRAS_API_KEY`).
- If `llm.api_key_env` uses a custom name, Aider tasks still work, but Jaunt has to remap the key under a process-wide lock, so those Aider tasks serialize.
- Architect-mode retries are stateful: Jaunt reuses the previous candidate source instead of starting from an empty file again.
- When an architect retry fails because diff-style edits do not apply cleanly, Jaunt falls back to whole-file editor repairs rather than repeating the same fragile SEARCH/REPLACE path.
- For narrow type-check or public-API contract fixes, Jaunt uses a smaller whole-file repair pass instead of paying for another full architect cycle.
- Aider-generated tests stay public-API-first and may add 1-2 direct contract-adjacent cases, not a large speculative matrix.

## Decorator Reference

### `@jaunt.magic(*deps, prompt=None, infer_deps=None)`

- `deps`: Explicit dependencies. Accepts objects, strings (`"pkg.mod:Name"` or `"pkg.mod.Name"`), or a list.
- `prompt`: Extra text appended to the LLM prompt for this spec.
- `infer_deps`: Per-spec override of AST-based dependency inference (`True`/`False`).

### `@jaunt.test(*deps, prompt=None, infer_deps=None, public_api_only=True)`

Same kwargs as `@magic`. Test function names must start with `test_`.

- `public_api_only=True` is the default. Generated tests should exercise the production API contract, not wrapper internals or generated-module internals.
- Use `public_api_only=False` only when the user intentionally wants white-box tests.

## Configuration (`jaunt.toml`)

```toml
version = 1

[paths]
source_roots = ["src"]           # Directories with @magic specs
test_roots = ["tests"]           # Directories with @test specs
generated_dir = "__generated__"  # Output subdirectory name

[llm]
provider = "openai"              # "openai" | "anthropic" | "cerebras"
model = "gpt-5.2"               # Model name
api_key_env = "OPENAI_API_KEY"   # Env var holding the API key

# Optional LLM tuning
reasoning_effort = "medium"                    # OpenAI/Cerebras: "low"|"medium"|"high"
anthropic_thinking_budget_tokens = 1024        # Anthropic extended thinking
max_cost_per_build = 10.0                      # Cost budget in USD

[agent]
engine = "legacy"               # or "aider"

[aider]
build_mode = "architect"
test_mode = "code"
skill_mode = "code"
editor_model = ""
map_tokens = 0
save_traces = false

[build]
jobs = 8                         # Parallel generation jobs
infer_deps = true                # Auto-infer dependencies from AST

[test]
jobs = 4
infer_deps = true
pytest_args = ["-q"]             # Extra pytest arguments

[prompts]
# Override default prompt templates (file paths relative to project root)
build_system = ""
build_module = ""
test_system = ""
test_module = ""
```

## CLI Commands

| Command | Purpose |
|---------|---------|
| `jaunt build` | Generate implementations for `@jaunt.magic` specs |
| `jaunt test` | Generate tests for `@jaunt.test` specs and run pytest |
| `jaunt init` | Scaffold `jaunt.toml` + directories |
| `jaunt clean` | Remove all `__generated__/` directories |
| `jaunt status` | Show stale vs fresh modules |
| `jaunt watch` | Auto-rebuild on file changes (`--test` to also run tests) |
| `jaunt eval` | Benchmark LLM providers on built-in cases |
| `jaunt skill build <name>` | Elaborate a checked-in user skill using the configured runtime |
| `jaunt skill refresh` | Refresh Jaunt-managed auto-generated skills |
| `jaunt cache info` | Show LLM response cache stats |
| `jaunt cache clear` | Clear cached LLM responses |

**Common flags:** `--root`, `--config`, `--jobs N`, `--force`, `--target MODULE`, `--no-infer-deps`, `--no-progress`, `--no-cache`, `--json`.

**Exit codes:** `0` success, `2` config/discovery/cycle error, `3` generation error, `4` pytest failure.

## Dependency System

Dependencies are declared via `deps=` and optionally inferred from AST.

```python
@jaunt.magic()
def base_func() -> str: ...

@jaunt.magic(deps=[base_func])
def higher_func() -> str:
    """Uses base_func internally."""
    ...
```

- Canonical ref format: `"pkg.mod:Qualname"`
- Dot shorthand: `"pkg.mod.Qualname"` (auto-converted)
- Objects: pass the function/class directly (e.g., `deps=[base_func]`)
- Circular dependencies are detected and block the build with exit code 2.

## Troubleshooting

1. **`JauntNotBuiltError` at runtime**: Run `jaunt build` first.
2. **Stale modules not rebuilding**: Check `jaunt status`. Dependency API changes include signature edits, full docstring contract edits, and whole-class member/method changes. Use `--force` to force regeneration.
3. **Dependency cycle error**: Check `deps=` declarations for circular references. Restructure specs to break the cycle.
4. **Generation error (exit 3)**: Review the spec docstring for ambiguity. Add `prompt=` for extra guidance. Check LLM API key and quota.
5. **Test failures (exit 4)**: Review generated tests in `__generated__/`. Refine test spec docstrings for clarity. If Aider mode added a direct extra edge case, decide whether the spec should explicitly keep or forbid that scenario.
6. **Missing API key**: Set the env var from `[llm].api_key_env` or add it to a `.env` file in the project root.
7. **Aider feels single-threaded**: Check whether `llm.api_key_env` is using a custom variable name. In Aider mode that forces a serialized env-var remap path.

## Anti-patterns to Avoid

- Vague docstrings: "Does X" without specifying semantics or edge cases.
- Editing `__generated__/` files by hand.
- Hidden global behavior in specs (env vars, implicit network, file reads).
- Over-constraining: forcing implementation details not required by the product.
- Writing implementations inside `@jaunt.magic` stubs (the body should just `raise RuntimeError`).
- Skipping test specs: always pair implementation specs with test specs.
- Expecting Aider mode to infer a large test matrix from a sparse test spec. It may add 1-2 obvious contract-adjacent cases, not broad unstated coverage.

## Reference

For CLI flags and command patterns, see `references/cli.md`.
For spec-writing examples, see `references/examples.md`.
