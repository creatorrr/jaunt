---
id: "110"
title: MCP Server
status: done
priority: 6
effort: medium
depends: ["030", "060"]
---

# TASK-110: MCP Server

## Problem

Jaunt has no programmatic interface for agents. The current approach is
"run `jaunt build` in the terminal," which works but loses context and
requires parsing CLI output.

## Deliverables

### Built-in MCP server

```bash
jaunt mcp serve              # start MCP server (stdio transport)
```

### Tools to expose

| Tool | Description |
|------|-------------|
| `jaunt_build` | Build specs, return structured results |
| `jaunt_test` | Generate and run tests |
| `jaunt_status` | Check which modules are stale |
| `jaunt_spec_info` | Return specs and dependency graph for a module |
| `jaunt_clean` | Clean generated files |

### Integration

- Use `fastmcp` for the Python MCP server
- Reuse existing CLI logic — each tool calls the same functions as the CLI
- Return structured JSON (same shape as `--json` output from TASK-030)
- Support configuration via `jaunt.toml`:
  ```toml
  [mcp]
  enabled = true
  ```

### Agent configuration

Users add to their Claude Code config:

```json
{
  "mcpServers": {
    "jaunt": {
      "command": "jaunt",
      "args": ["mcp", "serve"]
    }
  }
}
```

## Implementation Notes

- Depends on TASK-030 (JSON output) for structured return values
- Depends on TASK-060 (status, clean) for those tools
- `fastmcp` should be an optional dependency: `jaunt[mcp]`
- The MCP server runs in the project directory, reusing `jaunt.toml` config
- Consider adding a `resources` endpoint that exposes the spec graph
