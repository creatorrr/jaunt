---
id: "080"
title: Error Messages & DX
status: todo
priority: 5
effort: small-medium
depends: ["040"]
---

# TASK-080: Error Messages & DX

## Problem

Error messages are bare strings. When generation fails, the user sees
"No source returned" or a raw traceback. No suggestion of what to do next.
The config module uses manual TOML parsing with no schema validation.

## Deliverables

### Actionable error messages

Every error path should include:
- What went wrong (specific)
- Why (if determinable)
- What to do about it

Examples:

- `"Generation failed for my_app.specs: OpenAI returned empty content.
   Check your API key and model name, or run with --force to retry."`
  instead of `"OpenAI returned empty content."`

- `"Spec my_app.specs:normalize_email depends on my_app.utils:parse_domain,
   which failed to build. Fix the dependency first."`
  instead of `"Dependency failed: my_app.utils"`

- `"Module 'my_app.helpers' not found. Check that it exists under one of
   your source_roots: ['src', '.']. Did you mean 'my_app.helper'?"`

### Config validation

Convert config module from manual TOML parsing to dataclass-based validation:

- Validate `provider` is one of the supported values at parse time
- Validate `api_key_env` references a non-empty env var at config load
  (fail fast instead of at LLM call time)
- Validate `generated_dir` is a valid Python identifier
- Report all validation errors at once, not just the first

### Debug flags

- Add `--verbose` flag: show which modules are stale and why, dependency
  graph resolution, prompt sizes
- Add `--debug` flag: dump full prompts to stderr (or a file) for inspection

## Implementation Notes

- Error message improvements can be incremental
- Config validation is a good candidate for Pydantic if it's kept as a dep
- Debug output should go to stderr, never stdout (preserve JSON mode)
