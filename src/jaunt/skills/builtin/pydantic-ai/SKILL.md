---
name: "pydantic-ai"
description: "Use when generating LLM agent code with pydantic-ai — Agent construction, tools, typed dependencies, structured result types, and running agents sync/async."
---

# pydantic-ai

## What it is
pydantic-ai is a Python framework for building LLM agents with Pydantic types. Use it when
generated code should define an agent, register tools, pass typed dependencies, and return
structured validated results instead of free-form text.

Keep the agent declaration close to its result type and tools. Application code should call a
small wrapper rather than scattering prompts and tool registration across the codebase.

## Core concepts
- `Agent(...)` defines the model, system prompt, dependency type, and result or output type.
- Tools are Python functions registered with `@agent.tool` when they need context or
  `@agent.tool_plain` when they do not.
- `RunContext[T]` provides typed access to dependencies during tool execution.
- Result types are commonly Pydantic models so model output is validated.
- Agents can be run asynchronously with `await agent.run(...)` or synchronously with
  `agent.run_sync(...)`.

## Common patterns
Define dependencies and a structured result:

```python
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext


@dataclass
class SupportDeps:
    customer_id: str
    plan: str


class SupportReply(BaseModel):
    answer: str
    escalate: bool


support_agent = Agent(
    "openai:gpt-4.1-mini",
    deps_type=SupportDeps,
    result_type=SupportReply,
    system_prompt="Answer support questions using the available customer context.",
)
```

Register typed tools that use dependencies:

```python
@support_agent.tool
async def current_plan(ctx: RunContext[SupportDeps]) -> str:
    return ctx.deps.plan


async def answer_question(question: str, deps: SupportDeps) -> SupportReply:
    result = await support_agent.run(question, deps=deps)
    return result.data
```

Use `run_sync()` only from synchronous code paths such as scripts or tests that are not
already inside an event loop.

## Gotchas
- Do not call `run_sync()` from async web handlers. Use `await agent.run(...)`.
- Keep dependencies serializable or simple to fake; tools should receive clients through
  dependency objects rather than importing global clients.
- Tool functions should be deterministic where practical and should validate IDs, paths, and
  tenant access before returning data to the model.
- Structured result models should be small and strict. Very large schemas make model output
  harder to satisfy and tests harder to reason about.
- Model provider strings and output-type parameter names may differ across installed
  versions; follow the version pinned by the project.

## Testing notes
Test tools as normal Python functions by constructing a `RunContext` or factoring pure logic
out of the tool. Mock agent runs at your application boundary for unit tests. For integration
tests, use a test model or deterministic fake rather than calling a live provider. Assert that
the wrapper returns the Pydantic result type and handles validation or model errors cleanly.
