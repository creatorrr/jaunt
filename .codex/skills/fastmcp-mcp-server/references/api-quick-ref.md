# FastMCP API Quick Reference

## Server Constructor

```python
FastMCP(
    name="ServerName",                    # Required: human-readable name
    instructions="What this server does", # Optional: helps clients understand capabilities
    version="1.0.0",                      # Optional: server version
    mask_error_details=True,              # Production: hide internal errors
    on_duplicate_tools="error",           # "error" | "warn" | "replace" | "ignore"
    on_duplicate_resources="warn",        # "error" | "warn" | "replace" | "ignore"
    on_duplicate_prompts="replace",       # "error" | "warn" | "replace"
    strict_input_validation=False,        # True = reject type coercion (e.g. "10" for int)
    lifespan=my_lifespan_fn,             # Async context manager for startup/shutdown
    auth=auth_provider,                   # Auth provider (HTTP/SSE only)
)
```

## Decorators

### @mcp.tool
```python
@mcp.tool                                  # Basic
@mcp.tool(name="custom_name")              # Custom name
@mcp.tool(description="Override docstring") # Custom description
@mcp.tool(tags={"admin", "write"})         # Tags for filtering
@mcp.tool(timeout=30.0)                    # Timeout in seconds
@mcp.tool(version="2.0")                   # Version identifier
@mcp.tool(annotations={                    # Safety hints
    "title": "Display Name",
    "readOnlyHint": True,                  # Tool only reads data
    "destructiveHint": False,              # Changes are reversible
    "idempotentHint": True,                # Repeated calls = same effect
    "openWorldHint": False,                # No external system interaction
})
```

### @mcp.resource
```python
@mcp.resource("data://static")                     # Static URI
@mcp.resource("data://users/{user_id}")             # Template with parameter
@mcp.resource("files://{filepath*}")                # Wildcard (multi-segment)
@mcp.resource("search://results{?query,limit}")     # Query parameters
@mcp.resource("data://file", mime_type="text/csv")  # Explicit MIME type
@mcp.resource("data://config", tags={"public"})     # Tags for filtering
```

### @mcp.prompt
```python
@mcp.prompt                                 # Basic
@mcp.prompt(name="custom_name")             # Custom name
@mcp.prompt(description="Override docs")    # Custom description
@mcp.prompt(tags={"analysis"})              # Tags
```

## Context Object

Available via type hint in any tool/resource/prompt function:

```python
from fastmcp import Context

@mcp.tool
async def my_tool(arg: str, ctx: Context) -> str:
    # Logging
    await ctx.debug("Debug message")
    await ctx.info("Info message")
    await ctx.warning("Warning message")
    await ctx.error("Error message")

    # Progress
    await ctx.report_progress(progress=50, total=100)

    # Read resources
    resources = await ctx.list_resources()
    content = await ctx.read_resource("data://my-resource")

    # LLM sampling (requires client support)
    response = await ctx.sample("Summarize this text", temperature=0.7)

    # User elicitation (v2.10.0+)
    result = await ctx.elicit("What is your name?", response_type=str)

    # Metadata
    ctx.request_id       # Current request ID
    ctx.client_id        # Client identifier
    ctx.session_id       # Session identifier

    # Lifespan state
    ctx.lifespan_context  # Dict yielded by lifespan function
```

## Error Handling

```python
from fastmcp.exceptions import ToolError

raise ToolError("User-facing error message")  # Always shown to client
raise ValueError("Internal error")             # Hidden when mask_error_details=True
```

## Return Types

| Return Type | MCP Content Type |
|-------------|-----------------|
| `str` | `TextContent` |
| `bytes` | Base64 `BlobResourceContents` |
| `Image(path=...) / Image(data=...)` | `ImageContent` |
| `Audio(path=...) / Audio(data=...)` | `AudioContent` |
| `list[...]` | Multiple content blocks |
| `dict` / dataclass / Pydantic model | JSON `TextContent` + `structuredContent` |
| `None` | Empty response |
| `ToolResult(...)` | Full control over content + structured + meta |

## Client (for testing)

```python
from fastmcp import Client

async with Client(transport=mcp) as client:
    # Tools
    tools = await client.list_tools()
    result = await client.call_tool("tool_name", {"arg": "value"})

    # Resources
    resources = await client.list_resources()
    content = await client.read_resource("uri://resource")

    # Prompts
    prompts = await client.list_prompts()
    messages = await client.get_prompt("prompt_name", {"arg": "value"})
```

## Running

```python
# Python
mcp.run()                                          # stdio (default)
mcp.run(transport="http", host="0.0.0.0", port=8000)  # HTTP
mcp.run(transport="sse", host="0.0.0.0", port=8000)   # SSE (legacy)
```

```bash
# CLI
fastmcp run server.py:mcp
fastmcp run server.py:mcp --transport http --port 8000
fastmcp run server.py:mcp --reload  # dev mode with auto-reload
```

## Composition

```python
# Mount (dynamic link — sub-server changes propagate)
main.mount(sub_server, namespace="prefix")

# Import (static copy — snapshot at import time)
main.import_server(sub_server, namespace="prefix")

# Proxy remote server
from fastmcp import create_proxy
main.mount(create_proxy("http://remote:8000/mcp"), namespace="remote")
```

## Visibility Control

```python
mcp.disable(keys={"tool:admin_action"})     # Disable by key
mcp.disable(tags={"admin"})                  # Disable by tag
mcp.enable(tags={"public"}, only=True)       # Allowlist mode
```

## Auth Providers (HTTP only)

```python
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.workos import AuthKitProvider

# JWT verification
auth = JWTVerifier(jwks_uri="...", issuer="...", audience="...")

# OAuth proxy (GitHub, Google, Azure)
auth = GitHubProvider(client_id="...", client_secret="...", base_url="...")

# External IdP with DCR (WorkOS, Descope)
auth = AuthKitProvider(authkit_domain="...", base_url="...")

mcp = FastMCP("Server", auth=auth)
```

## Custom HTTP Routes

```python
@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})
```
