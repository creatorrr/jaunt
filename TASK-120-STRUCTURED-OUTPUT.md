---
id: "120"
title: Structured Output for Generation
status: todo
priority: 7
effort: medium
depends: ["010"]
---

# TASK-120: Structured Output for Generation

## Problem

The LLM outputs raw Python and Jaunt strips markdown fences with a regex.
No schema enforcement, no structured extraction. The "retry with error
context" is limited to syntax + symbol presence checks.

## Deliverables

### Use structured output / tool use

For providers that support it, request structured output:

**Anthropic (tool_use):**
```python
tools = [{
    "name": "write_module",
    "input_schema": {
        "type": "object",
        "properties": {
            "python_source": {"type": "string"},
            "imports_used": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "string"}
        },
        "required": ["python_source"]
    }
}]
```

**OpenAI (structured output / function calling):**
```python
response_format = {
    "type": "json_schema",
    "json_schema": {
        "name": "module_output",
        "schema": {
            "type": "object",
            "properties": {
                "python_source": {"type": "string"},
                "imports_used": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["python_source"]
        }
    }
}
```

### Fallback

Fall back to raw text + fence stripping for providers that don't support
structured output (e.g., a `litellm` backend wrapping a local model).

### Benefits

- Eliminates fence-stripping regex hack
- Enables richer metadata extraction (imports used, generation notes)
- More reliable parsing — no ambiguity about where the code starts/ends
- Can enforce output constraints at the provider level

## Implementation Notes

- Add a `supports_structured_output` property to `GeneratorBackend`
- Each backend implements structured output if available, raw text otherwise
- The `_strip_markdown_fences` fallback stays for backwards compatibility
- Test both paths (structured and raw) for each backend
