# Sliding Window Rate Limiter (Jaunt Example)

This example is a classic "API backend" building block: rate limiting per key
in a rolling time window. It is annoying to implement correctly because you
need to constantly prune expired timestamps, avoid off-by-one errors at the
window boundary, and keep memory bounded by removing keys once their windows
empty out. A fake clock makes the correctness testable without sleeping.

## Commands

Build:

```bash
uv run jaunt build --root examples/rate_limiter
```

Test:

```bash
PYTHONPATH=examples/rate_limiter/src uv run jaunt test --root examples/rate_limiter
```

