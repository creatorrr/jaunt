# Jaunt Skill (for AI Assistants)

## 1. What Is Jaunt
Jaunt is a workflow where humans and AI assistants write **intent** (Python spec stubs + tests), then Jaunt generates the **implementation**; humans review both the specs and the generated code, iterate, and re-run generation.

## 2. Your Role As An AI Assistant
- Help the human author, refine, and organize **spec stubs** and **test specs**.
- Ask clarifying questions when intent is underspecified (edge cases, I/O, errors, performance).
- Do **not** hand-write implementations for any symbol marked as “generated” (for example, `@jaunt.magic`), unless the human explicitly asks you to bypass Jaunt and accepts the tradeoff.

## 3. Workflow You Should Guide
1. Write or refine spec stubs in the user’s codebase (signatures + docstrings + type hints).
2. Write or refine test specs (pytest-style, deterministic, no network).
3. Run generation (typically `jaunt build`).
4. Review the generated output together (correctness, style, safety, performance).
5. Iterate: adjust specs/tests and regenerate.

Use `jaunt status` as the freshness check. Dependency API changes include signature edits, full docstring contract edits, and, for whole-class specs, class member or method-signature changes.

## 4. Writing Good Spec Stubs (most important)

### Principles
- **Be explicit about behavior.** Define inputs, outputs, invariants, and what “correct” means.
- **Specify failures.** Name the exception type and the error condition (or return shape for errors).
- **Define edge cases.** Empty inputs, `None`, boundary values, duplicates, ordering, timeouts.
- **Constrain the solution when it matters.** Complexity, determinism, caching, stable ordering.
- **Prefer pure logic.** Move I/O behind parameters (dependency injection) so tests stay fast and local.
- **Treat the full docstring as the contract.** The later rules and error cases matter, not just the first sentence.

### Patterns
- **Docstring as contract:** include short examples, preconditions, postconditions.
- **Whole-class specs are real API surfaces:** class members and method signatures are part of what downstream specs depend on.
- **Typed dependencies:** accept `Callable[...]` or protocol-like objects instead of reaching for globals.
- **Small, composable units:** one concept per symbol.

### Anti-patterns
- Vague docstrings: “Does X” without semantics.
- Hidden global behavior: environment variables, implicit network calls, reading files implicitly.
- Over-constraining early: forcing implementation details that are not required by the product.

### Templates

#### Pure Function
```python
from __future__ import annotations

# @jaunt.magic  (symbol is generated; do not implement by hand)
def normalize_email(raw: str) -> str:
    """
    Normalize an email address for stable comparisons.

    Rules:
    - Strip surrounding whitespace.
    - Lowercase the whole string.
    - Must contain exactly one "@".

    Errors:
    - Raise ValueError if the input is not a valid email by these rules.
    """
```

#### Function With Dependencies (I/O behind parameters)
```python
from __future__ import annotations

from collections.abc import Callable

# @jaunt.magic
def get_display_name(user_id: int, fetch_user: Callable[[int], dict]) -> str:
    """
    Return a user's display name.

    - fetch_user(user_id) returns a dict with keys: "first_name", "last_name", optional "nickname".
    - Prefer nickname if present and non-empty.
    - Otherwise return "first_name last_name" with single spaces.

    Errors:
    - Raise KeyError if required keys are missing.
    """
```

#### Stateful Class
```python
from __future__ import annotations

from dataclasses import dataclass

# @jaunt.magic
@dataclass
class RateLimiter:
    """
    Token bucket limiter.

    Parameters:
    - capacity: max tokens in the bucket (>= 1)
    - refill_per_second: tokens refilled per second (> 0)

    Behavior:
    - allow(now: float) -> bool consumes 1 token if available at time `now` and returns True.
    - If no token is available, return False and do not go negative.
    """

    capacity: int
    refill_per_second: float

    def allow(self, now: float) -> bool:
        """See class docstring."""
```

#### Async Function
```python
from __future__ import annotations

from collections.abc import Awaitable, Callable

# @jaunt.magic
async def retry(
    op: Callable[[], Awaitable[object]],
    *,
    attempts: int,
    base_delay_s: float,
) -> object:
    """
    Retry an async operation with exponential backoff.

    - Run op() up to `attempts` times (attempts >= 1).
    - Delay sequence: base_delay_s * (2 ** (i-1)) for retries i=1..(attempts-1).
    - If op() succeeds, return its result.

    Errors:
    - Re-raise the last exception if all attempts fail.
    """
```

## 5. Writing Good Test Specs

### Principles
- **Deterministic:** no network, no clock unless injected or controlled.
- **Small and focused:** one behavioral assertion per test when practical.
- **Prefer black-box behavior:** test the contract, not implementation details.
- **Include negative tests:** errors and invalid input paths.

### Patterns
- Table-driven tests for edge cases.
- Dependency injection with tiny fakes.
- Property-like checks where helpful (idempotence, monotonicity, stability).

### Anti-patterns
- Tests that depend on file system layout or external services.
- Snapshot tests of entire generated files (too brittle).

## 6. Configuration Reference (`jaunt.toml`)
`jaunt.toml` configures what modules to scan, where to write generated code, and which backend/settings to use. Keep it minimal and explicit; avoid hidden defaults when onboarding a repo.

See `examples/jaunt.toml` for a starter template.

## 7. Critical Rules
- Never edit `__generated__/` by hand (it will be overwritten).
- Always regenerate via the Jaunt CLI after changing specs/tests.
- Always review generated output before shipping.
