---
name: jaunt-clean
description: "Clean Jaunt generated files. Remove all __generated__ directories. Use when the user wants to clean up generated code, start fresh, or remove build artifacts."
argument-hint: "[--dry-run]"
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash
---

# Clean Jaunt Generated Files

Run `jaunt clean` to remove all `__generated__/` directories from the project.

## Steps

1. Run the clean command:

```bash
uv run jaunt clean --json $ARGUMENTS
```

2. Report what was removed (or would be removed with `--dry-run`):
   - List of removed `__generated__/` directories
   - Number of files cleaned

3. If using `--dry-run`, remind the user that no files were actually deleted.

## Common Usage

- `/jaunt-clean` — Remove all `__generated__/` directories
- `/jaunt-clean --dry-run` — Preview what would be removed without deleting
