---
name: jaunt
description: "Use when working with the Jaunt spec-driven code generation framework for Python. Trigger for requests mentioning Jaunt, @jaunt.magic, @jaunt.test, spec stubs, jaunt build, jaunt test, jaunt.toml, __generated__ directories, or writing specs/tests that Jaunt will generate implementations for. Also use when the user wants to set up a new Jaunt project, configure LLM providers, debug build failures, or understand the spec-driven development workflow."
user-invocable: false
disable-model-invocation: false
---

# Jaunt (spec-driven code generation)

Jaunt is a spec-driven code generation framework for Python: you write intent as
`@jaunt.magic` / `@jaunt.test` / `@jaunt.contract` stubs and Jaunt generates real
implementations and pytest tests into `__generated__/` using the OpenAI Codex CLI.

## Get the current workflow primer

Jaunt ships its own always-current, **project-aware** agent briefing. Run it and
load the output into context before working:

```bash
jaunt instructions          # markdown primer + a live snapshot of THIS project
jaunt instructions --json   # {command, ok, text, project} for tooling/MCP
```

`jaunt instructions` is the single source of truth for how to author specs, run
the build/test loop, read build status, and use contract mode. This file is kept
intentionally thin so it cannot drift from the installed CLI.

## Hard rules (if you cannot run the CLI)

- **Never edit files under `__generated__/`** — they are overwritten on every build.
- **Never hand-write the body of a `@jaunt.magic` symbol.** The docstring is the
  contract and Jaunt fills in the code; keep the body a `raise RuntimeError(...)`
  stub.
- **Pair every `@jaunt.magic` spec with `@jaunt.test` coverage.**
