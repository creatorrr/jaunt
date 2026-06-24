---
name: codex-harness
description: >-
  Use when driving OpenAI's Codex CLI programmatically or embedding it as a
  code-writing harness — non-interactive `codex exec` (scripting/CI), `codex
  mcp-server` (in-process MCP client, the embedding path), `codex app-server`
  (JSON-RPC for deep integration), or via the OpenAI Agents SDK. Also for
  building a Jaunt GeneratorBackend on top of Codex (the eventual aider
  replacement). Triggers: codex exec, codex mcp-server, codex app-server, cdx,
  scripting/driving Codex, replacing aider with codex.
---

# Codex as a Code-Writing Harness

OpenAI's **Codex CLI** is a coding agent that writes, edits, reviews, and runs
code. Unlike a library you call, Codex is an *agent process*: you give it a
prompt and a workspace, and it iterates (reads files, runs commands in a
sandbox, edits files) until the task is done. This skill covers the four ways to
drive it programmatically, and how to embed it as Jaunt's code-generation
backend.

Reference docs (load on demand):

| File | Mode | Use it for |
|------|------|-----------|
| [references/exec.md](references/exec.md) | `codex exec` | One-shot, scriptable runs; CI; piping; JSONL streams |
| [references/mcp-server.md](references/mcp-server.md) | `codex mcp-server` | **Embedding path** — reusable, thread-based sessions over MCP |
| [references/app-server.md](references/app-server.md) | `codex app-server` | Deep IDE-grade integration: JSON-RPC, approvals, fine-grained events |
| [references/agents-sdk.md](references/agents-sdk.md) | OpenAI Agents SDK | Multi-agent orchestration with Codex as a tool |
| [references/config-and-auth.md](references/config-and-auth.md) | (cross-cutting) | config.toml, profiles, sandbox, features, auth |
| [references/jaunt-integration.md](references/jaunt-integration.md) | (Jaunt) | A Codex `GeneratorBackend` — the aider parallel |

Version this skill was written against: **codex-cli 0.142.0**. Codex moves fast;
`app-server` is explicitly experimental. When in doubt, regenerate the protocol
schema (`codex app-server generate-json-schema --out <dir>`) or read
`codex <subcommand> --help` rather than trusting a doc.

## Modes at a glance

```
codex exec        Non-interactive one-shot. Prompt in (arg/stdin), work happens,
                  process exits. --json gives a JSONL event stream. Best for
                  scripts, CI, and "fire a task, read the result."

codex mcp-server  Codex speaks the Model Context Protocol over stdio. Exposes two
                  tools: `codex` (start a session) and `codex-reply` (continue a
                  thread). A long-lived process you talk to as an MCP client.
                  This is OpenAI's recommended *embedding* path.

codex app-server  Codex speaks bidirectional JSON-RPC 2.0 (stdio / unix / ws).
                  Full surface: threads, turns, items, approvals, fs APIs,
                  config, model list. Powers the IDE extensions. Most control,
                  most surface area, experimental.

Agents SDK        Not a Codex mode — the OpenAI Agents SDK launches `codex
                  mcp-server` as an MCPServerStdio and lets agents call it,
                  with handoffs and tracing.
```

## Which mode for which job

- **"Run a task and read what it did."** → `codex exec`. Simplest. Subprocess,
  `--json` for structured events, `--output-last-message` / `--output-schema`
  for a clean result. This is what `cdx` (your alias) wraps.
- **"Embed Codex in a long-running Python process; reuse sessions; one task →
  follow-up."** → `codex mcp-server`. Thread-based, structured tool I/O, one
  process serves many calls. **The Jaunt harness uses this.**
- **"I need approvals, per-item events, fs/exec control, or I'm building an
  editor-like UI."** → `codex app-server`.
- **"Orchestrate several specialized agents that each delegate coding to
  Codex."** → Agents SDK (which itself uses `mcp-server` under the hood).

## Prerequisites

```bash
codex --version          # codex-cli 0.142.0 (this skill's baseline)
codex doctor             # diagnose install / config / auth / runtime health
codex login              # ChatGPT auth → ~/.codex/auth.json
# or: export CODEX_API_KEY=sk-...   (API-key auth for a single invocation)
```

Auth, model selection, sandbox, and features are **cross-cutting** — they work
the same across every mode and are documented once in
[references/config-and-auth.md](references/config-and-auth.md).

## Cross-cutting gotchas (read before you script anything)

1. **`codex mcp` ≠ `codex mcp-server`.** `codex mcp` *manages external* MCP
   servers that Codex connects to (`add`/`list`/`remove`). `codex mcp-server`
   makes *Codex itself* an MCP server. Easy to conflate; very different.

2. **Two event vocabularies.** `codex exec --json` emits **dotted** events
   (`thread.started`, `turn.completed`, `item.completed`). `app-server` uses
   **slashed** JSON-RPC methods (`thread/start`, `turn/start`,
   `item/completed`). Don't mix them up when writing a parser.

3. **`auth.json` is a credential.** `~/.codex/auth.json` holds access tokens —
   treat it like a password. Never bake `CODEX_API_KEY` into a CI job that
   checks out untrusted code; prefer the Codex GitHub Action's proxy.

4. **Sandbox defaults to `read-only`.** A harness that needs Codex to *write*
   files must pass `--sandbox workspace-write` (exec) or `sandbox:
   "workspace-write"` (mcp/app-server). Otherwise edits silently can't land.

5. **The bypass flag is gated by the *outer* harness.** `codex exec
   --dangerously-bypass-approvals-and-sandbox` is what you use inside an
   already-sandboxed runner. When Codex is invoked from *inside another agent*
   (e.g. Claude Code's auto mode), that outer harness may block the bypass flag
   and refuse to let an agent self-add the allow-rule — a **human** must add
   `"Bash(codex exec --dangerously-bypass-approvals-and-sandbox:*)"` to the
   project's permission allowlist. See [references/exec.md](references/exec.md).

6. **`app-server` is experimental.** Method names and shapes change between
   versions. Generate the schema/types from your installed binary rather than
   hardcoding from a doc (see [references/app-server.md](references/app-server.md)).
