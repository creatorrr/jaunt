# `codex mcp-server` — Codex as an MCP Server

`codex mcp-server` runs Codex as a **Model Context Protocol** server over stdio.
A long-lived process you talk to as an MCP *client*: it exposes two tools and
keeps session state, so one process serves many calls. This is OpenAI's
recommended way to **embed** Codex (the Agents SDK uses it under the hood), and
the path the Jaunt harness is built on.

> Don't confuse `codex mcp-server` (Codex *is* the server) with `codex mcp`
> (manage *external* MCP servers Codex connects to).

```bash
codex mcp-server                      # stdio MCP server
codex mcp-server -c model="gpt-5.5" --enable multi_agent
```

Flags are limited to config plumbing: `-c key=value`, `--enable/--disable
<FEATURE>`, `--strict-config`. Everything behavioral (model, sandbox, cwd,
approvals) is passed **per call** as tool arguments.

## The two tools

### `codex` — start a session

Starts a Codex conversation. Parameters mirror the Codex `Config` struct:

| Param | Type | Notes |
|-------|------|-------|
| `prompt` | string | **Required.** The initial user prompt. |
| `model` | string | e.g. `gpt-5.5`. |
| `cwd` | string | Working dir for the session. Relative paths resolve against the server process's cwd. |
| `sandbox` | `read-only` \| `workspace-write` \| `danger-full-access` | **Pass `workspace-write` to let Codex edit files.** |
| `approval-policy` | `untrusted` \| `on-failure` \| `on-request` \| `never` | For headless use, `never`. |
| `base-instructions` | string | Replace the default system instructions. |
| `developer-instructions` | string | Injected as a developer-role message. |
| `compact-prompt` | string | Prompt used when compacting the conversation. |
| `config` | object | Arbitrary `config.toml` overrides (same keys as `-c`). |

The call is **long-running** — Codex iterates until the turn is done. Progress
arrives as MCP notifications during the call. The tool result's
`structuredContent` carries the **thread id** (commonly `threadId`) plus the
final agent message; capture the id to continue the session.

### `codex-reply` — continue a thread

| Param | Type | Notes |
|-------|------|-------|
| `prompt` | string | **Required.** The next user prompt. |
| `threadId` | string | **Required.** From the prior call's `structuredContent`. |
| `conversationId` | string | *Deprecated* alias for `threadId`. |

## Driving it from Python (MCP SDK)

The lightest dependency is the official `mcp` Python SDK — a stdio client + a
`ClientSession`. No Agents SDK needed.

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(command="codex", args=["mcp-server"])

async def run():
    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Optional: see the tool schemas the server advertises.
            tools = await session.list_tools()

            # Start a session. Let Codex write to the repo, no approvals.
            res = await session.call_tool("codex", {
                "prompt": "Implement the TODOs in src/foo.py and run pytest.",
                "cwd": "/path/to/repo",
                "sandbox": "workspace-write",
                "approval-policy": "never",
                "model": "gpt-5.5",
            })

            # The thread id lives in structuredContent. Inspect the actual shape
            # once — keys can vary by version.
            sc = res.structuredContent or {}
            thread_id = sc.get("threadId") or sc.get("thread_id")

            # Continue the same thread.
            await session.call_tool("codex-reply", {
                "threadId": thread_id,
                "prompt": "Now add tests for the new code.",
            })

asyncio.run(run())
```

### Streaming progress

Codex emits MCP **notifications** (logging / progress) while a `codex` or
`codex-reply` call runs. Register handlers on the `ClientSession`
(`logging_callback` / progress handler, per the SDK version) if you want live
events; otherwise the final state is in the tool result. For a high-fidelity,
typed event stream (per-item deltas, approvals), use `app-server` instead — see
[app-server.md](app-server.md).

## Why this is the embedding path

- **One process, many tasks.** Start the server once; fan many generations
  through it. Cheaper than a fresh `codex exec` per task.
- **Thread-native.** `threadId` makes "generate → critique → fix" a first-class
  loop without re-seeding context.
- **Structured I/O.** Tool args/results are JSON, not scraped stdout.
- **Sandbox per call.** Every `codex` call sets its own `cwd` + `sandbox`, so one
  server can safely serve isolated workspaces.

For the worked Jaunt `GeneratorBackend` built on this, see
[jaunt-integration.md](jaunt-integration.md).
