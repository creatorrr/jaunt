---
id: "070"
title: Prompt Quality
status: done
priority: 4
effort: medium
depends: ["020", "010"]
---

# TASK-070: Prompt Quality

## Problem

The prompts are minimal. The system prompt is ~8 lines. There's no guidance on
code style, error handling, import conventions, or how to use the dependency
context that TASK-020 now provides. The prompts don't constrain output style.

## Deliverables

### Improve prompt templates

Key additions to `build_system.md` and `build_module.md`:

- **Import conventions:** "Import from `{generated_module}` for dependency
  symbols. Use the module paths shown in the dependency context."
- **Style constraints:** "Include type hints matching the spec signatures.
  Add brief docstrings. Follow PEP 8. Target Python 3.12+."
- **Error handling:** "Preserve exception types specified in spec docstrings.
  Don't catch exceptions that should propagate."
- **Dependency usage:** "Here is the generated source for your dependencies.
  Import and call their symbols using the module paths shown."
- **Anti-patterns:** "Don't add unused imports. Don't import the spec module's
  own symbols. Don't generate test code."

### User-level prompt customization

Allow inline prompt additions in `jaunt.toml`:

```toml
[prompts]
extra_system = """
Always use `logging` instead of `print`.
Prefer dataclasses over plain dicts.
"""
```

- Parse `extra_system` from config
- Append to system prompt after the standard template
- Keep existing file-path overrides for full template replacement

### Test prompt rendering

- Add snapshot tests for rendered prompts
- When a prompt template changes, the test shows the diff in what the LLM sees

## Implementation Notes

- Prompt engineering is iterative — this task may span multiple PRs
- Test against both build and test prompt paths
- Consider provider-specific prompt tweaks (e.g., Anthropic prefers XML tags)
