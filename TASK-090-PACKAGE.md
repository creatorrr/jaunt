---
id: "090"
title: Package & Distribution
status: todo
priority: 5
effort: small
depends: ["010"]
---

# TASK-090: Package & Distribution

## Problem

`openai` is a hard dependency even if you never use it. The core library
(decorators, config, discovery, digest) shouldn't require any LLM SDK.

## Deliverables

### Make all LLM SDKs optional

```toml
[project]
dependencies = []   # core has no LLM deps

[project.optional-dependencies]
openai = ["openai>=1.0.0,<2"]
anthropic = ["anthropic>=0.39.0,<1"]
all = ["jaunt[openai]", "jaunt[anthropic]"]
```

### Guard SDK imports

The OpenAI backend already does a lazy import inside `__init__`. Ensure it
raises a clear `JauntConfigError` (like the Anthropic backend already does)
when the SDK is missing:

```
The 'openai' package is required for provider='openai'.
Install it with: pip install jaunt[openai]
```

### Update install docs

- README should show `pip install jaunt[openai]` or `pip install jaunt[anthropic]`
- `jaunt init` (TASK-060) should suggest the right extras based on chosen provider

## Implementation Notes

- This is mostly a `pyproject.toml` change + adding an import guard to `openai_backend.py`
- Test suite already mocks OpenAI calls, so removing the hard dep shouldn't
  break tests — but verify the import guard works
- Currently partially done: anthropic is already optional, openai is still required
