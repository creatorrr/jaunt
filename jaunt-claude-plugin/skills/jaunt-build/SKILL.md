---
name: jaunt-build
description: "Build Jaunt specs. Run jaunt build to generate implementations from @jaunt.magic decorated stubs. Use when the user wants to build, generate, or compile their Jaunt specs."
argument-hint: "[--force] [--target MODULE]"
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash
---

# Build Jaunt Specs

Run `jaunt build` to generate implementations from `@jaunt.magic` decorated spec stubs.

## Steps

1. Run the build command with any user-provided arguments:

```bash
uv run jaunt build --json $ARGUMENTS
```

2. Parse the JSON output and report results to the user:
   - **Generated modules**: Modules that were (re)generated
   - **Skipped modules**: Modules that were already up-to-date
   - **Failed modules**: Modules that failed generation (include error details)

3. If there are failures:
   - Show the error message for each failed module
   - Suggest refining the spec docstring or adding `prompt=` for extra guidance
   - Check that the API key env var is set

## Common Usage

- `/jaunt-build` — Build all stale modules
- `/jaunt-build --force` — Force full regeneration (ignore digest cache)
- `/jaunt-build --target my_app.specs` — Build a specific module only
- `/jaunt-build --jobs 16` — Override parallelism
- `/jaunt-build --no-cache` — Skip LLM response cache

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 2 | Config, discovery, or dependency cycle error |
| 3 | Code generation error |
