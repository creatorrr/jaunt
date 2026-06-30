---
name: "pytest"
description: "Use when writing pytest tests or test-friendly code. Covers fixtures, parametrize, exception and async testing (pytest-asyncio / anyio), and structuring deterministic tests with no network or wall-clock dependency."
---

# pytest

## What it is
pytest is Jaunt's test runner. Tests must be deterministic and isolated.

## Core concepts
- `assert` statements (no special assert methods); `pytest.raises(ExcType)` for errors.
- `@pytest.mark.parametrize` for table-driven cases; fixtures for shared setup.
- Async: `@pytest.mark.asyncio` (asyncio) or `@pytest.mark.anyio` (anyio) per the configured runner.

## Common patterns
- One behavioral assertion per test when practical; descriptive `test_` names.
- Inject clocks/IO via parameters; never call the network.

## Gotchas
- Don't depend on test execution order or global state.
- Avoid `time.sleep`; avoid real timestamps unless injected.

## Testing notes
- Cover the negative paths (invalid input, raised exceptions), not just the happy path.
