---
name: jaunt-init
description: "Initialize a new Jaunt project. Scaffold jaunt.toml configuration file and source/test directories. Use when the user wants to start a new Jaunt project or set up Jaunt in an existing codebase."
argument-hint: "[--force]"
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash
---

# Initialize a Jaunt Project

Run `jaunt init` to scaffold a new Jaunt project with configuration and directory structure.

## Steps

1. Run the init command:

```bash
uv run jaunt init --json $ARGUMENTS
```

2. Report what was created:
   - `jaunt.toml` configuration file
   - Source and test directories

3. After initialization, remind the user to:
   - Edit `jaunt.toml` to configure their LLM provider and API key
   - Set the `[llm].provider` (`"openai"`, `"anthropic"`, or `"cerebras"`)
   - Set the `[llm].model` to their preferred model
   - Set the `[llm].api_key_env` to the env var name holding their API key
   - Start writing spec stubs with `@jaunt.magic()` in the source root

## Common Usage

- `/jaunt-init` — Create `jaunt.toml` and directories
- `/jaunt-init --force` — Overwrite existing `jaunt.toml`
