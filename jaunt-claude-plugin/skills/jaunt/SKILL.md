---
name: jaunt
description: "Use when working with the Jaunt spec-driven code generation framework for Python. Trigger for requests mentioning Jaunt, @jaunt.magic, @jaunt.test, spec stubs, jaunt build, jaunt test, jaunt.toml, __generated__ directories, or writing specs/tests that Jaunt will generate implementations for. Also use when the user wants to set up a new Jaunt project, configure LLM providers, debug build failures, or understand the spec-driven development workflow."
user-invocable: false
disable-model-invocation: false
---

# Jaunt (spec-driven code generation)

## Overview

Jaunt is a Python framework where humans write **intent** as decorator-marked stubs and Jaunt generates **implementations** via an LLM backend. The core loop: write specs, write tests, run `jaunt build`, review generated code, iterate.

Your role as an AI assistant: help author and refine spec stubs and test specs. **Do not** hand-write implementations for `@jaunt.magic` symbols unless the user explicitly asks to bypass Jaunt.

## Repo Triage (Do First)

1. Check for `jaunt.toml` at the project root. If missing, the project needs `jaunt init`.
2. Identify `[paths]` in `jaunt.toml`: `source_roots` (where specs live), `test_roots` (where test specs live), `generated_dir` (output directory name, usually `__generated__`).
3. Identify the LLM provider: `[llm].provider` is `"openai"`, `"anthropic"`, or `"cerebras"`. The API key env var is in `[llm].api_key_env`.
4. Check for existing `__generated__/` directories to see what has already been built.
5. Run `jaunt status` to see which modules are stale vs fresh.

## Writing Spec Stubs (`@jaunt.magic`)

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
- The docstring is the contract; make it decision-complete.

**Spec patterns:**

Function with dependencies:
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

Async function:
```python
@jaunt.magic()
async def retry(op: Callable[[], Awaitable[object]], *, attempts: int, base_delay_s: float) -> object:
    """Retry op() with exponential backoff. Re-raise last exception if all fail."""
    raise RuntimeError("spec stub (generated at build time)")
```

## Writing Test Specs (`@jaunt.test`)

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
```

**Principles for good test specs:**
- Deterministic: no network, no clock unless injected.
- Small and focused: one behavioral assertion per test when practical.
- Black-box behavior: test the contract, not implementation details.
- Include negative tests: errors and invalid input paths.
- Name must start with `test_`.

## Decorator Reference

### `@jaunt.magic(*deps, prompt=None, infer_deps=None)`

- `deps`: Explicit dependencies. Accepts objects, strings (`"pkg.mod:Name"` or `"pkg.mod.Name"`), or a list.
- `prompt`: Extra text appended to the LLM prompt for this spec.
- `infer_deps`: Per-spec override of AST-based dependency inference (`True`/`False`).

### `@jaunt.test(*deps, prompt=None, infer_deps=None)`

Same kwargs as `@magic`. Test function names must start with `test_`.

## Configuration (`jaunt.toml`)

```toml
version = 1

[paths]
source_roots = ["src"]
test_roots = ["tests"]
generated_dir = "__generated__"

[llm]
provider = "openai"              # "openai" | "anthropic" | "cerebras"
model = "gpt-5.2"
api_key_env = "OPENAI_API_KEY"

[build]
jobs = 8
infer_deps = true

[test]
jobs = 4
infer_deps = true
pytest_args = ["-q"]
```

## Anti-patterns to Avoid

- Vague docstrings: "Does X" without specifying semantics or edge cases.
- Editing `__generated__/` files by hand (they will be overwritten).
- Hidden global behavior in specs (env vars, implicit network, file reads).
- Over-constraining: forcing implementation details not required by the product.
- Writing implementations inside `@jaunt.magic` stubs (the body should just `raise RuntimeError`).
- Skipping test specs: always pair implementation specs with test specs.

## Troubleshooting

1. **`JauntNotBuiltError` at runtime**: Run `jaunt build` first.
2. **Stale modules not rebuilding**: Check `jaunt status`. Use `--force` to force regeneration.
3. **Dependency cycle error**: Check `deps=` declarations for circular references.
4. **Generation error (exit 3)**: Review the spec docstring for ambiguity. Add `prompt=` for extra guidance.
5. **Test failures (exit 4)**: Review generated tests in `__generated__/`. Refine test spec docstrings.
6. **Missing API key**: Set the env var from `[llm].api_key_env` or add it to a `.env` file.

## Reference

For CLI flags and command patterns, see `references/cli.md`.
For spec-writing examples, see `references/examples.md`.
