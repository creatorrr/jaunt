---
id: "140"
title: Eval Suite & Testing Infrastructure
status: todo
priority: 9
effort: medium
depends: ["070"]
---

# TASK-140: Eval Suite & Testing Infrastructure

## Problem

No integration test that actually calls an LLM. The test suite patches
everything. There's no way to verify that prompt changes (TASK-070) actually
improve generation quality.

## Deliverables

### Eval suite

A set of spec → expected-behavior pairs that can be run against a real backend:

```bash
jaunt eval                    # run evals against configured backend
jaunt eval --model gpt-4o    # compare models
jaunt eval --provider anthropic --model claude-sonnet-4-5-20250929
```

Eval cases:
- Simple function (pure logic, no deps) — baseline
- Function with type hints and complex return type
- Class with methods and properties
- Module with dependencies on another module
- Module using an external library (e.g., pydantic, requests)

Each eval case defines:
- The spec stub(s)
- Assertions on the generated code (runs, passes type check, produces
  expected output for given inputs)

### Eval results storage

Store results in `.jaunt/evals/` for comparison:

```
.jaunt/evals/
  2025-01-15T10:30:00/
    summary.json              # pass/fail counts, model, provider
    cases/
      simple_function.json    # generated code, assertions, pass/fail
      class_with_deps.json
```

### Prompt snapshot tests

When you change a prompt template, the test fails and shows the diff in
what the LLM would actually see. Approve the diff to update the snapshot.

- Store snapshots in `tests/snapshots/`
- Use `pytest --snapshot-update` to approve changes
- Cover both build and test prompt rendering

### Model comparison

```bash
jaunt eval --compare gpt-4o claude-sonnet-4-5-20250929
```

Run the same eval suite against multiple models and output a comparison table.

## Implementation Notes

- Evals require API keys — run separately from the main test suite
- Use `pytest -m eval` marker to isolate eval tests
- Snapshot tests can run without API keys (they test prompt rendering, not LLM output)
- Consider using the examples/ directory as a source of eval cases
