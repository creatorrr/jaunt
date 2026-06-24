# Codex with the OpenAI Agents SDK

The OpenAI **Agents SDK** doesn't add a new Codex mode — it launches `codex
mcp-server` as an `MCPServerStdio` and lets one or more agents call Codex's
tools, with handoffs and tracing for free. Use this when you want *several*
specialized agents (PM, frontend, backend, tester) that each delegate the actual
coding to Codex, rather than a single embedding call.

## Pattern

Codex runs as an MCP subprocess; the SDK wires its `codex` / `codex-reply` tools
into your agents.

```python
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

async def main():
    async with MCPServerStdio(
        name="Codex CLI",
        params={"command": "codex", "args": ["mcp-server"]},
        client_session_timeout_seconds=360_000,   # Codex turns can run long
    ) as codex_mcp_server:
        agent = Agent(
            name="Developer",
            instructions="Build software by delegating coding tasks to Codex.",
            mcp_servers=[codex_mcp_server],
        )
        await Runner.run(agent, "Create a Snake game in Python with tests.")
```

The agent sees two tools from the server:

- **`codex`** — start a session (`prompt`, plus `model`, `cwd`, `sandbox`,
  `approval-policy`, `base-instructions`, …).
- **`codex-reply`** — continue using the `threadId` returned in the prior call's
  `structuredContent`.

The full tool schemas are documented in [mcp-server.md](mcp-server.md).

## Single-agent vs multi-agent

- **Single-agent:** one `Agent` calls `codex` directly. Simplest; good when the
  orchestration logic is trivial and you mainly want tracing.
- **Multi-agent:** a coordinator agent hands off to specialized agents
  (designer / frontend / backend / tester), each invoking `codex` with a scoped
  task and its own `cwd`. Handoffs and the full call tree show up in the OpenAI
  trace dashboard.

## Guardrails for unattended runs

For deterministic, auditable execution pass these on every `codex` tool call:

```python
{ "approval-policy": "never", "sandbox": "workspace-write" }
```

`never` means Codex won't pause for human approval; `workspace-write` confines
edits to the workspace. Combine with per-agent `cwd` to isolate work.

## When to prefer this vs. raw mcp-server

- **Just embedding Codex in your own Python control flow** (e.g. a Jaunt
  backend) → talk to `mcp-server` directly with the MCP SDK; you don't need the
  Agents SDK layer. See [jaunt-integration.md](jaunt-integration.md).
- **Orchestrating multiple LLM agents** where Codex is one capability among many,
  and you want SDK handoffs + tracing → use the Agents SDK as above.
