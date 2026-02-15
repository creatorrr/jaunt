# MCP Server Build Checklist & Patterns

## Build Checklist

### 1. Define the server's purpose
- What domain does this server cover? (DB access, API integration, file management, etc.)
- Who are the clients? (Claude Desktop, IDE extensions, custom agents)
- What transport? (stdio for local, HTTP for network/multi-client)

### 2. Design tools (most critical step)
- List 5-15 focused tools with clear verb_noun names.
- For each tool, decide: read-only or mutating? Idempotent?
- Write the docstring first — it's what the LLM reads.
- Use specific types and `Field()` constraints for parameters.
- Plan error cases and what `ToolError` messages to show.

### 3. Design resources (if applicable)
- Identify read-only data the client may need.
- Use URI templates for parameterized resources.
- Set appropriate MIME types.

### 4. Design prompts (if applicable)
- Create reusable message templates for common workflows.
- Parameterize with typed arguments.

### 5. Implement server
- Create `FastMCP` instance with descriptive name and instructions.
- Implement tools with `async def` for I/O operations.
- Use lifespan for shared state (DB connections, HTTP clients).
- Set `mask_error_details=True` for production.

### 6. Write tests
- Install `pytest` + `pytest-asyncio`.
- Use `Client(transport=mcp)` for in-process testing.
- Test each tool with valid inputs, edge cases, and error cases.
- Test resource reads and prompt generation.

### 7. Configure deployment
- STDIO: add `if __name__ == "__main__": mcp.run()`.
- HTTP: choose port, add health check route, configure auth.
- Document Claude Desktop config JSON.

### 8. Security review
- No secrets in tool descriptions or error messages.
- Input validation on all parameters.
- Path traversal protection for file tools.
- Auth configured for network-facing servers.
- `mask_error_details=True` enabled.

---

## Common Patterns

### Database tool server

```python
from contextlib import asynccontextmanager
import asyncpg
from fastmcp import FastMCP, Context
from fastmcp.exceptions import ToolError

@asynccontextmanager
async def db_lifespan(server):
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    try:
        yield {"pool": pool}
    finally:
        await pool.close()

mcp = FastMCP("DatabaseServer", lifespan=db_lifespan, mask_error_details=True)

@mcp.tool(annotations={"readOnlyHint": True})
async def query(sql: str, ctx: Context) -> list[dict]:
    """Run a read-only SQL query. Only SELECT statements are allowed."""
    if not sql.strip().upper().startswith("SELECT"):
        raise ToolError("Only SELECT queries are allowed.")
    pool = ctx.lifespan_context["pool"]
    rows = await pool.fetch(sql)
    return [dict(r) for r in rows]
```

### API wrapper server

```python
import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

mcp = FastMCP("APIWrapper", mask_error_details=True)

@mcp.tool
async def search_api(query: str, limit: int = 10) -> list[dict]:
    """Search the external API for results."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.example.com/search",
            params={"q": query, "limit": limit},
            headers={"Authorization": f"Bearer {os.environ['API_KEY']}"},
        )
        if resp.status_code != 200:
            raise ToolError(f"API returned status {resp.status_code}")
        return resp.json()["results"]
```

### File manager server

```python
from pathlib import Path
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

ALLOWED_ROOT = Path("/data/workspace")

mcp = FastMCP("FileManager", mask_error_details=True)

def safe_path(path_str: str) -> Path:
    """Resolve path and ensure it's within the allowed root."""
    resolved = (ALLOWED_ROOT / path_str).resolve()
    if not resolved.is_relative_to(ALLOWED_ROOT):
        raise ToolError("Access denied: path outside allowed directory.")
    return resolved

@mcp.tool(annotations={"readOnlyHint": True})
def list_files(directory: str = ".") -> list[str]:
    """List files in a directory within the workspace."""
    target = safe_path(directory)
    return [str(p.relative_to(ALLOWED_ROOT)) for p in target.iterdir()]

@mcp.tool(annotations={"readOnlyHint": True})
def read_file(path: str) -> str:
    """Read a text file from the workspace."""
    return safe_path(path).read_text()

@mcp.tool(annotations={"destructiveHint": False})
def write_file(path: str, content: str) -> str:
    """Write content to a file in the workspace."""
    target = safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"Written {len(content)} bytes to {path}"
```

### Composed gateway server

```python
from fastmcp import FastMCP, create_proxy

gateway = FastMCP("Gateway")

# Mount local sub-servers
gateway.mount(db_server, namespace="db")
gateway.mount(file_server, namespace="files")

# Mount remote servers
gateway.mount(create_proxy("http://analytics:8000/mcp"), namespace="analytics")

# Add health check (HTTP transport only)
@gateway.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("OK")

if __name__ == "__main__":
    gateway.run(transport="http", port=8000)
```

### Test file template

```python
import pytest
from fastmcp import Client
from my_server import mcp

@pytest.fixture
async def client():
    async with Client(transport=mcp) as c:
        yield c

async def test_tool_list(client):
    tools = await client.list_tools()
    names = {t.name for t in tools}
    assert "greet" in names

async def test_tool_success(client):
    result = await client.call_tool("greet", {"name": "World"})
    assert "Hello, World" in result[0].text

async def test_tool_validation_error(client):
    with pytest.raises(Exception):
        await client.call_tool("greet", {})

async def test_resource(client):
    resources = await client.list_resources()
    assert len(resources) > 0
```
