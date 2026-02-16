---
name: jaunt-test
description: "Run Jaunt tests. Generate tests from @jaunt.test stubs and run pytest. Use when the user wants to test their Jaunt project, generate test implementations, or run the test suite."
argument-hint: "[--force] [--no-build] [--no-run]"
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash
---

# Run Jaunt Tests

Run `jaunt test` to generate test implementations from `@jaunt.test` spec stubs and execute them with pytest.

## Steps

1. Run the test command with any user-provided arguments:

```bash
uv run jaunt test --json $ARGUMENTS
```

2. Parse the JSON output and report results to the user:
   - **Build phase**: Which implementation modules were built (if `--no-build` not set)
   - **Test generation**: Which test modules were generated
   - **Pytest results**: Pass/fail status and test output

3. If tests fail (exit code 4):
   - Show the pytest output summary
   - Suggest reviewing the generated tests in `__generated__/`
   - Suggest refining the test spec docstrings for clarity

4. If generation fails (exit code 3):
   - Show error details for failed modules
   - Suggest checking spec docstrings and API key

## Common Usage

- `/jaunt-test` — Build + generate tests + run pytest
- `/jaunt-test --no-build` — Skip implementation build, just generate and run tests
- `/jaunt-test --no-run` — Generate test files without running pytest
- `/jaunt-test --force` — Force regeneration of all tests
- `/jaunt-test --target my_app.specs` — Test a specific module only

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 2 | Config, discovery, or dependency cycle error |
| 3 | Code generation error |
| 4 | Pytest failure |
