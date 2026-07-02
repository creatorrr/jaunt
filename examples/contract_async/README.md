# Async contract mode example

Contract mode covers async functions, not just sync ones. The committed async
body is the source of truth; the docstring is the contract, and Jaunt derives a
committed pytest battery (with `pytest-asyncio` auto mode) instead of generating
the body.

```bash
# Add @jaunt.contract and derive the battery (deterministic — call-form Examples,
# no API key needed):
jaunt adopt amod:fetch_slug

# Gate on it (deterministic, offline, no API key):
jaunt check
```

The derived `test_examples` / `test_raises_valueerror` functions are `async def`
and `await` the contract function.
