---
name: jaunt-status
description: "Check Jaunt module status. Show which spec modules are stale vs fresh. Use when the user wants to see what needs rebuilding, check build freshness, or understand project state."
disable-model-invocation: false
user-invocable: true
allowed-tools: Bash
---

# Check Jaunt Module Status

Run `jaunt status` to see which spec modules are stale (need rebuilding) vs fresh (up-to-date).

## Steps

1. Run the status command:

```bash
uv run jaunt status --json $ARGUMENTS
```

2. Parse the JSON output and report to the user:
   - **Stale modules**: Modules whose specs have changed since last build
   - **Fresh modules**: Modules that are up-to-date
   - If there are stale modules, suggest running `jaunt build` or `/jaunt-build`

## Common Usage

- `/jaunt-status` — Show all module statuses
- `/jaunt-status --target my_app.specs` — Check a specific module
