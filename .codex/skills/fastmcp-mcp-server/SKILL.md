---
name: fastmcp-mcp-server
description: Build production-ready MCP (Model Context Protocol) servers using Python and FastMCP. Use when creating tools, resources, or prompts for AI agents; designing server architecture; adding auth; testing MCP servers; deploying via stdio/HTTP; or composing multiple servers. Trigger for requests mentioning MCP, FastMCP, MCP server, MCP tools, MCP resources, model context protocol, or building integrations for Claude/LLM agents.
---

# MCP Server Development (FastMCP)

## Overview

Use this skill to design and build MCP servers using FastMCP — the standard Python framework for the Model Context Protocol. MCP lets you expose tools, resources, and prompts to LLM applications (Claude Desktop, IDEs, custom agents) via a standardized protocol.

**Current versions** (as of Feb 2026):
- MCP Spec: `2025-11-25` (latest)
- FastMCP stable: `2.14.5`
- FastMCP 3.0: `3.0.0rc2` (release candidate — pin to `<3` for production)

## Project Setup

### Installation

```bash
# Stable (recommended for production)
uv add 'fastmcp<3'

# Or with pip
pip install 'fastmcp<3'

# Latest RC (opt-in to new features)
uv add 'fastmcp==3.0.0rc2'
```

### Minimal server

```python
from fastmcp import FastMCP

mcp = FastMCP("MyServer")

@mcp.tool
def greet(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"

if __name__ == "__main__":
    mcp.run()
```

### Project structure (recommended)

```
my_mcp_server/
  server.py          # FastMCP instance + tool/resource/prompt definitions
  pyproject.toml      # Dependencies (fastmcp, etc.)
  tests/
    test_server.py    # Pytest tests using Client
```

## Core Concepts

MCP servers expose three component types to clients:

| Component | Purpose | Decorator |
|-----------|---------|-----------|
| **Tools** | Functions the LLM can execute (actions, queries, computations) | `@mcp.tool` |
| **Resources** | Read-only data the client can request (files, configs, DB records) | `@mcp.resource("uri://...")` |
| **Prompts** | Reusable message templates for structured LLM interactions | `@mcp.prompt` |

## Tools (Most Important)

Tools are the primary way LLMs interact with your server. Design them carefully.

### Basic tool

```python
@mcp.tool
def calculate_sum(a: float, b: float) -> float:
    """Add two numbers together."""
    return a + b
```

FastMCP auto-generates the JSON schema from the function signature, name, and docstring. Always write clear docstrings — they are the LLM's primary guide.

### Async tools (preferred for I/O)

```python
@mcp.tool
async def fetch_user(user_id: str) -> dict:
    """Fetch user details from the database."""
    async with get_db() as db:
        return await db.get_user(user_id)
```

Use `async def` for network calls, DB queries, and file I/O. Sync functions run in a threadpool automatically but async is preferred.

### Parameter documentation

```python
from typing import Annotated
from pydantic import Field

@mcp.tool
def search(
    query: Annotated[str, "The search query string"],
    limit: int = Field(10, description="Max results to return", ge=1, le=100),
    category: str | None = None,
) -> list[dict]:
    """Search the product catalog."""
    ...
```

Use `Annotated[T, "description"]` for simple docs, or `Field()` for validation constraints.

### Error handling

```python
from fastmcp.exceptions import ToolError

@mcp.tool
def divide(a: float, b: float) -> float:
    """Divide a by b."""
    if b == 0:
        raise ToolError("Cannot divide by zero.")
    return a / b
```

- `ToolError`: Always shown to the client (use for expected, user-facing errors)
- Other exceptions: Logged server-side; details hidden when `mask_error_details=True`

For production, mask internal errors:
```python
mcp = FastMCP("SecureServer", mask_error_details=True)
```

### Tool annotations (safety hints)

```python
@mcp.tool(
    annotations={
        "title": "Delete Record",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def delete_record(record_id: str) -> str:
    """Permanently delete a record."""
    ...
```

Annotations help clients decide whether to auto-approve or prompt the user. Mark read-only tools as `readOnlyHint: True` to skip confirmation prompts.

### Timeouts

```python
@mcp.tool(timeout=30.0)
async def slow_operation(data: str) -> dict:
    """Process data (may take up to 30 seconds)."""
    ...
```

### Dependency injection (hidden parameters)

```python
from fastmcp.dependencies import Depends

def get_api_client() -> APIClient:
    return APIClient(api_key=os.environ["API_KEY"])

@mcp.tool
def call_api(query: str, client: APIClient = Depends(get_api_client)) -> dict:
    """Query the external API."""
    return client.search(query)  # client is injected, not exposed to LLM
```

### Context access

```python
from fastmcp import Context

@mcp.tool
async def process_data(uri: str, ctx: Context) -> dict:
    """Process data from a resource with progress reporting."""
    await ctx.info(f"Processing {uri}")
    resource = await ctx.read_resource(uri)
    await ctx.report_progress(progress=50, total=100)

    # Request LLM sampling (if client supports it)
    summary = await ctx.sample(f"Summarize: {resource[:500]}")

    await ctx.report_progress(progress=100, total=100)
    return {"summary": summary.text}
```

Context methods: `debug()`, `info()`, `warning()`, `error()`, `report_progress()`, `read_resource()`, `sample()`, `elicit()`.

### Structured output

```python
from dataclasses import dataclass

@dataclass
class AnalysisResult:
    score: float
    category: str
    details: list[str]

@mcp.tool
def analyze(text: str) -> AnalysisResult:
    """Analyze text sentiment."""
    return AnalysisResult(score=0.85, category="positive", details=["upbeat tone"])
```

Return type annotations generate JSON schemas for structured output.

## Resources

Resources provide read-only data access.

### Static resource

```python
@mcp.resource("config://app/settings")
def get_settings() -> str:
    """Return current application settings."""
    return json.dumps({"theme": "dark", "language": "en"})
```

### Resource template (parameterized)

```python
@mcp.resource("users://{user_id}/profile")
def get_user_profile(user_id: str) -> str:
    """Get a user's profile by ID."""
    return json.dumps(load_profile(user_id))
```

Clients request `users://alice/profile` — FastMCP extracts `user_id="alice"`.

### Binary resources

```python
@mcp.resource("files://{path}", mime_type="application/octet-stream")
def get_file(path: str) -> bytes:
    """Read a file as binary."""
    return Path(path).read_bytes()
```

## Prompts

Prompts are reusable message templates.

```python
from fastmcp.prompts import Message

@mcp.prompt
def code_review(code: str, language: str = "python") -> list[Message]:
    """Generate a code review prompt."""
    return [
        Message(role="user", content=f"Review this {language} code:\n\n```{language}\n{code}\n```"),
        Message(role="assistant", content="I'll review the code for correctness, style, and potential issues."),
        Message(role="user", content="Focus on security vulnerabilities and performance."),
    ]
```

## Server Configuration

```python
mcp = FastMCP(
    name="ProductionServer",
    instructions="A server for managing customer data and analytics.",
    version="1.2.0",
    mask_error_details=True,           # Hide internal errors from clients
    on_duplicate_tools="error",         # Fail on duplicate tool names
    on_duplicate_resources="warn",      # Warn on duplicate resource URIs
)
```

## Lifespan (Startup/Shutdown)

Manage shared state like DB connections:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def app_lifespan(server):
    db = await Database.connect(os.environ["DATABASE_URL"])
    try:
        yield {"db": db}
    finally:
        await db.disconnect()

mcp = FastMCP("MyServer", lifespan=app_lifespan)

@mcp.tool
async def query_db(sql: str, ctx: Context) -> list[dict]:
    """Run a read-only SQL query."""
    db = ctx.lifespan_context["db"]
    return await db.fetch_all(sql)
```

## Server Composition

### Mounting (dynamic, live link)

```python
from fastmcp import FastMCP

main = FastMCP("Main")
weather = FastMCP("Weather")
analytics = FastMCP("Analytics")

@weather.tool
def get_forecast(city: str) -> str:
    """Get weather forecast."""
    return f"Sunny in {city}"

main.mount(weather, namespace="weather")       # -> tool: weather_get_forecast
main.mount(analytics, namespace="analytics")
```

### Importing (static, one-time copy)

```python
main.import_server(weather, namespace="weather")  # Copies tools at import time
```

### Proxy remote servers

```python
from fastmcp import FastMCP, create_proxy

mcp = FastMCP("Gateway")
mcp.mount(create_proxy("http://api.example.com/mcp"), namespace="api")
```

## Transports

### STDIO (default — local, single-client)

```python
mcp.run()  # or mcp.run(transport="stdio")
```

Best for: Claude Desktop, local CLI tools, single-user.

### HTTP (Streamable — network, multi-client)

```python
mcp.run(transport="http", host="0.0.0.0", port=8000)
# Endpoint: http://localhost:8000/mcp
```

Best for: remote deployment, multi-client, web infrastructure.

### SSE (legacy — avoid for new projects)

```python
mcp.run(transport="sse", host="0.0.0.0", port=8000)
```

Only use for compatibility with older clients.

### CLI

```bash
fastmcp run server.py:mcp
fastmcp run server.py:mcp --transport http --port 8000
fastmcp run server.py:mcp --reload  # auto-reload during development
```

## Authentication (HTTP only)

### JWT verification (existing auth infra)

```python
from fastmcp.server.auth.providers.jwt import JWTVerifier

auth = JWTVerifier(
    jwks_uri="https://auth.example.com/.well-known/jwks.json",
    issuer="https://auth.example.com",
    audience="my-mcp-server",
)
mcp = FastMCP("SecureServer", auth=auth)
```

### OAuth proxy (GitHub, Google, Azure)

```python
from fastmcp.server.auth.providers.github import GitHubProvider

auth = GitHubProvider(
    client_id=os.environ["GITHUB_CLIENT_ID"],
    client_secret=os.environ["GITHUB_CLIENT_SECRET"],
    base_url=os.environ.get("BASE_URL", "http://localhost:8000"),
)
mcp = FastMCP("GitHubServer", auth=auth)
```

Auth only applies to HTTP/SSE transports. STDIO inherits local OS security.

## Testing

### Setup

```bash
uv add --dev pytest pytest-asyncio
```

In `pyproject.toml`:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

### Testing tools with Client

```python
import pytest
from fastmcp import Client

from my_server import mcp

@pytest.fixture
async def client():
    async with Client(transport=mcp) as c:
        yield c

async def test_greet(client):
    result = await client.call_tool("greet", {"name": "Alice"})
    assert result[0].text == "Hello, Alice!"

async def test_greet_missing_name(client):
    with pytest.raises(Exception):
        await client.call_tool("greet", {})

async def test_list_tools(client):
    tools = await client.list_tools()
    tool_names = [t.name for t in tools]
    assert "greet" in tool_names
```

### Testing resources

```python
async def test_settings_resource(client):
    result = await client.read_resource("config://app/settings")
    data = json.loads(result[0].text)
    assert "theme" in data
```

## Design Best Practices

### Tool design

1. **Keep tools focused** — one clear action per tool. Avoid "do everything" tools.
2. **Write excellent docstrings** — the LLM reads them to decide when/how to use the tool.
3. **Use specific types** — `int`, `float`, `str`, `bool`, enums, Pydantic models. Avoid `Any` or `dict`.
4. **Validate at the boundary** — use `Field()` constraints and raise `ToolError` for bad input.
5. **Separate read vs. write tools** — mark read-only tools with `readOnlyHint: True`.
6. **Don't overwhelm the agent** — prefer 5-15 well-designed tools over 50+ unfocused ones.

### Naming

- Tool names: verb_noun pattern (`get_user`, `create_order`, `search_products`)
- Resource URIs: hierarchical (`data://users/{id}/orders`)
- Prompts: descriptive (`code_review`, `summarize_document`)

### Security

1. Never embed secrets in tool descriptions or responses.
2. Use `mask_error_details=True` in production — prevents leaking stack traces.
3. Validate and sanitize all inputs — MCP tools are an attack surface.
4. Use auth for HTTP transports — never expose unauthenticated tools on the network.
5. Apply path restrictions for file-access tools — prevent directory traversal.
6. Rate limit expensive operations.

### Performance

1. Use `async def` for I/O-bound tools.
2. Set appropriate `timeout` values.
3. Use lifespan for connection pooling (DB, HTTP clients).
4. Consider mounting to split large servers into focused modules.

## Claude Desktop Integration

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/my-server", "python", "server.py"]
    }
  }
}
```

Or for HTTP servers:

```json
{
  "mcpServers": {
    "my-server": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

## Reference

See `references/patterns.md` for a complete server build checklist and common patterns.
See `references/api-quick-ref.md` for a concise API reference.
