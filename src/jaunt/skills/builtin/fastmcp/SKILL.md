---
name: "fastmcp"
description: "Use when generating MCP servers or clients with FastMCP — defining tools/resources/prompts with typed signatures, the FastMCP server lifecycle, and running over stdio/HTTP."
---

# fastmcp

## What it is
FastMCP is a Python framework for building Model Context Protocol servers and clients with
typed functions. Use it when generated code should expose tools, resources, or prompts to an
agent, or when tests need an in-process MCP client for a local server.

Good FastMCP code looks like a small typed API surface. Keep tool functions focused, validate
inputs with type annotations or Pydantic models, and keep transport setup at the edge.

## Core concepts
- `FastMCP("name")` creates a server instance.
- `@mcp.tool` exposes a callable action with typed parameters.
- `@mcp.resource("scheme://path")` exposes read-only contextual data.
- `@mcp.prompt` exposes reusable prompt templates.
- `mcp.run(...)` starts the server over a transport such as stdio or HTTP.
- `Client(...)` can call a server in tests or from another Python process.

## Common patterns
Define tools with precise signatures and docstrings:

```python
from fastmcp import FastMCP


mcp = FastMCP("billing")


@mcp.tool
def cents_to_dollars(cents: int) -> str:
    """Convert integer cents to a fixed two-decimal dollar string."""
    return f"{cents / 100:.2f}"
```

Expose stable resources with URI templates:

```python
@mcp.resource("users://{user_id}/profile")
def user_profile(user_id: str) -> dict[str, str]:
    return {"id": user_id, "status": "active"}
```

Run the server only from an entry point:

```python
if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Use an in-process client for integration-style tests:

```python
from fastmcp import Client


async def test_tool() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("cents_to_dollars", {"cents": 123})
        assert result.data == "1.23"
```

## Gotchas
- Tool arguments and return values must be serializable over MCP. Do not return open files,
  database connections, generators, or arbitrary class instances.
- Keep resource functions read-only. Mutations belong in tools.
- Long-running tools should use async functions, timeouts, and cancellation-aware I/O.
- Avoid starting a server at import time; decorators should register capabilities, while
  `mcp.run()` belongs under an entry-point guard.
- Treat all tool inputs as untrusted. Validate paths, shell commands, URLs, and tenant IDs
  before touching local files or external systems.

## Testing notes
Test tool functions directly for pure behavior, then add a small FastMCP client test for
registration and serialization. Tests should run in process when possible and should not
require a real network port. Assert names, schemas, error cases, and that resource functions
do not mutate state.
