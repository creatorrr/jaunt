# PROMPT-130: Example Project (Sliding Window Rate Limiter)

Repo: `/Users/ishitajindal/Documents/jaunt`

## Objective
Demonstrate a relatable “API backend” problem with tricky correctness: sliding windows, pruning, memory bounds, and a fake clock for testability.

## Owned Files (edit only these)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/rate_limiter/jaunt.toml` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/rate_limiter/src/limiter_demo/__init__.py` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/rate_limiter/src/limiter_demo/specs.py` (edit)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/rate_limiter/tests/__init__.py` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/rate_limiter/tests/specs.py` (edit)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/rate_limiter/README.md` (new)

## Hard Requirements (Do Not Skip)
Make files valid Python:
- Every `@jaunt.magic` stub must include:
  - `raise RuntimeError("spec stub (generated at build time)")`
- Every `@jaunt.test` stub must include:
  - `raise AssertionError("spec stub (generated at test time)")`

## Deliverables

### 1) `jaunt.toml`
Minimal config:
- `source_roots=["src"]`, `test_roots=["tests"]`, `generated_dir="__generated__"`
- OpenAI config with `OPENAI_API_KEY`, model `gpt-5.2`

### 2) `src/limiter_demo/__init__.py`
Export:
- `SlidingWindowLimiter`
- `Clock`

### 3) `src/limiter_demo/specs.py` (edit)
Keep the existing `Clock` protocol + `SlidingWindowLimiter` spec but ensure stub methods raise.
Keep the spec explicit about:
- pruning expired timestamps on each `allow()`
- pruning keys whose window is empty (memory bound)
- `remaining()` and `reset_at()` semantics

### 4) `tests/__init__.py`
Empty file so `tests` is a package.

### 5) `tests/specs.py` (edit)
Ensure each `@jaunt.test` stub raises.
Test intent should include:
- allows up to limit
- window expiry frees capacity (requires advancing fake clock)
- remaining count
- independent keys
- invalid constructor args

### 6) `README.md`
Include:
- Build/test commands:
  - `uv run jaunt build --root jaunt-examples/rate_limiter`
  - `PYTHONPATH=jaunt-examples/rate_limiter/src uv run jaunt test --root jaunt-examples/rate_limiter`
- One paragraph “why this is annoying to implement correctly”.

## Quality Gates
```bash
.venv/bin/python -m compileall jaunt-examples/rate_limiter/src jaunt-examples/rate_limiter/tests
```

## Constraints
- No external dependencies.
- Do not modify Jaunt core code.

