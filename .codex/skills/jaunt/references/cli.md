# Jaunt CLI Reference

Verify exact flags with: `jaunt --help`, `jaunt build --help`, etc.

## Commands

### build — Generate implementations
```bash
jaunt build                              # Build all stale modules, including API-invalidated dependents
jaunt build --force                      # Regenerate everything
jaunt build --jobs 16                    # Override parallelism
jaunt build --target my_app.specs        # Build one module
jaunt build --no-infer-deps              # Explicit deps only
jaunt build --no-cache                   # Skip LLM response cache
jaunt build --json                       # Machine-readable stdout
```

### test — Generate tests and run pytest
```bash
jaunt test                               # Build + generate tests + run
jaunt test --no-build                    # Skip build, just test
jaunt test --no-run                      # Generate tests without running
jaunt test --force                       # Force regen of tests
jaunt test --pytest-args=-k email        # Extra pytest args (repeatable)
jaunt test --json                        # Machine-readable output
```

### init — Scaffold a new project
```bash
jaunt init                               # Create jaunt.toml + dirs
jaunt init --force                       # Overwrite existing config
```

### clean — Remove generated directories
```bash
jaunt clean                              # Delete __generated__/ dirs
jaunt clean --dry-run                    # Preview what would be removed
```

### status — Show module staleness
```bash
jaunt status                             # Preview the same stale set build will regenerate
jaunt status --json                      # Machine-readable output
```

`status` is intended to reflect dependency-driven freshness too. If an upstream module's exported API changes, downstream dependents belong in the stale set even if their own source file did not change.

### watch — Auto-rebuild on changes
```bash
jaunt watch                              # Rebuild on file changes
jaunt watch --test                       # Also run tests after build
```

### eval — Benchmark LLM providers
```bash
jaunt eval                               # Run default codegen suite
jaunt eval --suite codegen               # Built-in codegen evals
jaunt eval --suite agent                 # End-to-end Aider/skills evals
jaunt eval --provider anthropic          # Override provider
jaunt eval --model gpt-4o               # Override model
jaunt eval --compare openai:gpt-4o anthropic:claude-sonnet-4-5-20250929
jaunt eval --case case_id                # Run specific case(s)
```

### skill — Manage skills
```bash
jaunt skill list                         # Show available skills
jaunt skill show rich                    # Print a skill
jaunt skill build rich                   # Elaborate a checked-in skill scaffold
jaunt skill refresh                      # Refresh Jaunt-managed auto skills
```

### cache — Manage LLM response cache
```bash
jaunt cache info                         # Show cache stats
jaunt cache clear                        # Clear all cached responses
```

## Global Flags (apply to build, test, status, watch)

```
--root PATH          Project root directory
--config PATH        Path to jaunt.toml
--jobs N             Override concurrency
--force              Force regeneration (ignore digests)
--target MODULE      Restrict to module(s), repeatable
--no-infer-deps      Use explicit deps only
--no-progress        Suppress progress bar
--no-cache           Bypass LLM response cache
--json               Machine-readable output to stdout
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 2 | Config, discovery, or dependency cycle error |
| 3 | Code generation error |
| 4 | Pytest failure |

## Environment

- API key: set env var from `[llm].api_key_env` in `jaunt.toml`
- `.env` file in project root is auto-loaded
- `JAUNT_GENERATED_DIR`: override the generated directory name at runtime

## Aider Runtime Notes

- Enable with `[agent].engine = "aider"` and install `jaunt[aider]`.
- The same commands are used in both runtimes; `jaunt watch` also respects the configured runtime.
- Watch cycles are sequential. Build/test work inside a cycle still uses normal Jaunt concurrency.
- For best Aider parallelism, keep `llm.api_key_env` on the canonical provider env var name.
- With a custom `llm.api_key_env`, Aider tasks stay correct but serialize while Jaunt remaps the key for Aider/litellm.
