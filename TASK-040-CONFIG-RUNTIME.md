---
id: "040"
title: Config & Runtime Fixes
status: done
priority: 2
effort: small
depends: []
---

# TASK-040: Config & Runtime Fixes

## Problem

- `runtime.py` hardcoded `generated_dir="__generated__"` — if a user set a
  custom `generated_dir` in `jaunt.toml`, the runtime couldn't find generated code
- OpenAI API calls had no retry logic for transient errors (429, 5xx, timeouts)
- No mechanism to propagate config to import-time runtime code

## Solution (Implemented)

### Runtime generated_dir fix
- Added `_get_generated_dir()` in `runtime.py` that reads `JAUNT_GENERATED_DIR`
  env var, falling back to `"__generated__"`
- Added `_sync_generated_dir_env()` in `cli.py` that sets the env var from config
  before any module imports happen
- Tests: `test_runtime_generated_dir.py`, `test_cli_sync_env.py`

### API retry with backoff
- `_call_openai()` now retries up to 4 times with exponential backoff (1s, 2s, 4s)
- Retries on: `RateLimitError`, `APITimeoutError`, `APIConnectionError`, 5xx status
- Non-retryable errors (400, 401, ValueError) fail immediately
- Same retry logic in the Anthropic backend
- Tests: `test_openai_retry.py`

## What Remains (Future)

- Convert config module from manual TOML parsing to Pydantic models (see TASK-080)
- Add `--verbose` / `--debug` flags for full tracebacks
