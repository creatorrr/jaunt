---
id: "130"
title: Caching & Cost Controls
status: done
priority: 8
effort: medium
depends: []
---

# TASK-130: Caching & Cost Controls

## Problem

Every `jaunt build --force` regenerates everything from scratch. No caching
of LLM responses. No visibility into token usage or cost.

## Deliverables

### Response caching

Cache LLM responses keyed on the full prompt hash. Store in `.jaunt/cache/`.

```bash
jaunt build --force           # skip staleness check, but still use cache
jaunt build --no-cache        # skip cache entirely
```

Cache key = SHA-256 of:
- System prompt (rendered)
- User prompt (rendered)
- Model name
- Provider name

If the cache hit has a valid entry, return it without calling the LLM.

### Cost tracking

Log token counts and estimated cost per build:

```
build: 3 modules generated (1,247 input tokens, 892 output tokens, ~$0.003)
```

- Extract token counts from provider response metadata
- Estimate cost based on known per-token pricing (configurable)
- Show summary at end of build (stderr, or in JSON output)

### Budget limits

Optional cost cap in config:

```toml
[llm]
max_cost_per_build = 1.00     # USD, optional
```

- Track cumulative cost during a build
- Abort if limit exceeded, reporting which modules were built and which skipped

### Cache management

```bash
jaunt cache info              # show cache size, entry count
jaunt cache clear             # delete all cached responses
```

## Implementation Notes

- Cache directory: `.jaunt/cache/` (add to `.gitignore` template in `jaunt init`)
- Cache format: one JSON file per entry, keyed by prompt hash
- `--force` should still use cache — it means "ignore digest staleness" not
  "ignore everything." `--no-cache` is the explicit cache bypass.
- Cost tracking depends on provider response metadata; may not be available
  for all providers
