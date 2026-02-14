---
id: "010"
title: Multi-Provider Backends
status: done
priority: 1
effort: medium
depends: []
---

# TASK-010: Multi-Provider Backends

## Problem

The only backend was OpenAI. `cli.py` hard-rejected anything else
(`if cfg.llm.provider != "openai": raise`). For a tool aimed at coding agents,
not supporting Anthropic is a dealbreaker.

## Solution (Implemented)

- Added `AnthropicBackend` in `src/jaunt/generate/anthropic_backend.py`
- Updated `_build_backend()` in `cli.py` to dispatch on `provider` field
- Anthropic SDK is an optional dependency: `pip install jaunt[anthropic]`
- Both backends share prompt templates, fence stripping, and retry logic
- Config supports `provider = "anthropic"` with `api_key_env = "ANTHROPIC_API_KEY"`

## What Remains (Future)

- Consider a `litellm` backend as a catch-all for local models, Ollama, Azure
- Make `openai` SDK itself optional (currently still a hard dep — see TASK-090)
