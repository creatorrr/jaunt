# `codex app-server` — JSON-RPC Protocol

`codex app-server` exposes Codex over **bidirectional JSON-RPC 2.0**. It's the
deepest integration surface — the one the VS Code extension is built on —
covering threads, turns, items, approvals, filesystem APIs, config, accounts,
and model management. **Experimental:** method names and shapes change between
versions, so generate the schema from your installed binary (below) rather than
trusting any prose, including this file.

Reach for app-server (over `mcp-server`) when you need: per-item streaming deltas
(`item/agentMessage/delta`, command output deltas), interactive **approvals**,
direct `command/exec` or `fs/*` control, or an editor-grade UI.

```bash
codex app-server                       # stdio (default), newline-delimited JSON
codex app-server --listen unix://       # unix socket
codex app-server --listen ws://127.0.0.1:1456   # websocket (experimental)
```

Transports (`--listen`): `stdio://` (default), `unix://[PATH]`, `ws://IP:PORT`,
or `off`. In websocket mode the server also serves HTTP health endpoints
(`/readyz`, `/healthz`) and supports capability-token / bearer auth. A running
TUI can attach to a remote app server via `codex --remote ws://...
--remote-auth-token-env TOKEN`.

Subcommands:
- `codex app-server daemon` — manage a local app-server daemon.
- `codex app-server proxy` — proxy stdio bytes to a running daemon's control socket.
- `codex app-server generate-ts --out <dir>` — TypeScript bindings.
- `codex app-server generate-json-schema --out <dir>` — JSON Schema bundle.

## Generate the authoritative protocol (do this first)

```bash
codex app-server generate-json-schema --out ./codex-schema
ls ./codex-schema
#   ClientRequest.json   (every request method + params)
#   ClientNotification.json
#   codex_app_server_protocol.schemas.json     (full bundle, ~550k)
#   codex_app_server_protocol.v2.schemas.json
#   ... plus a *.json per type

codex app-server generate-ts --out ./codex-ts   # same protocol as TS types
```

`ClientRequest.json` is the source of truth for method names and param shapes
for the version you have installed. Diff it across upgrades.

## Protocol primitives

- **Thread** — a conversation (`thr_...`).
- **Turn** — one request and the agent work it produces (`turn_...`).
- **Item** — a unit of input/output: agent message, reasoning, command
  execution, file change, tool call, plan.

Messages are standard JSON-RPC 2.0:

```jsonc
// Request: has method + id + params
{ "method": "thread/start", "id": 10, "params": { "model": "gpt-5.6-sol" } }
// Response: echoes id, with result or error
{ "id": 10, "result": { "thread": { "id": "thr_123" } } }
// Notification: no id (server → client events)
{ "method": "turn/started", "params": { "turn": { "id": "turn_456" } } }
```

> Note the **slashed** method names here vs. the **dotted** event names from
> `codex exec --json` (`thread.started`). Different vocabularies.

## Handshake → start a thread → send a turn

```jsonc
// 1. initialize, then announce initialized
{ "method": "initialize", "id": 0,
  "params": { "clientInfo": { "name": "jaunt", "title": "Jaunt", "version": "0.5.0" } } }
{ "method": "initialized", "params": {} }

// 2. create a conversation
{ "method": "thread/start", "id": 1, "params": { "model": "gpt-5.6-sol" } }
//    → { "id": 1, "result": { "thread": { "id": "thr_123" } } }

// 3. send user input; stream events back
{ "method": "turn/start", "id": 2,
  "params": { "threadId": "thr_123",
              "input": [{ "type": "text", "text": "Run the tests" }],
              "model": "gpt-5.6-sol", "effort": "medium" } }
```

After `turn/start` the server streams notifications until the turn completes.

## Method catalog (families, from the generated schema)

| Family | Representative methods |
|--------|------------------------|
| Lifecycle | `initialize`, `initialized` |
| Thread | `thread/start`, `thread/resume`, `thread/fork`, `thread/list`, `thread/read`, `thread/archive`, `thread/delete`, `thread/compact/start`, `thread/goal/{set,get,clear}`, `thread/metadata/update` |
| Turn | `turn/start`, `turn/steer`, `turn/interrupt` |
| Item events | `item/started`, `item/completed`, `item/agentMessage/delta`, `item/reasoning/textDelta`, `item/commandExecution/outputDelta`, `item/fileChange/patchUpdated`, `item/mcpToolCall/progress`, `item/plan/delta` |
| Approvals | `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval` (server → client; client replies with a decision) |
| Command/process | `command/exec`, `command/exec/outputDelta`, `command/exec/write`, `command/exec/terminate`, `process/spawn`, `process/outputDelta`, `process/exited` |
| Filesystem | `fs/watch`, `fs/unwatch`, `fs/changed`, `fs/readFile`, `fs/writeFile`, `fs/readDirectory`, `fs/createDirectory`, `fs/copy`, `fs/remove`, `fs/getMetadata` |
| Config | `config/read`, `config/value/write`, `config/batchWrite`, `config/mcpServer/reload` |
| Model | `model/list`, `model/rerouted`, `model/verification` |
| Account/auth | `account/read`, `account/login/{start,cancel,completed}`, `account/logout`, `account/rateLimits/read`, `account/usage/read` |
| Skills/apps | `skills/list`, `skills/config/write`, `app/list` (invoke via `$<name>` markers in input) |

(Exact set depends on your version — regenerate to confirm.)

## Approvals

When a turn needs to run a command or write a file under an approval policy, the
server sends an `item/.../requestApproval` request; the client responds with a
decision (`approve` / `deny`, sometimes "approve for session"). A headless
harness either runs with a non-asking policy or auto-approves these.

## Experimental & extras

- Opt into newer methods with `capabilities.experimentalApi: true` in
  `initialize`.
- Clients can suppress specific notifications per connection (reduce noise).
- MCP integration: app-server can call tools on configured external MCP servers.

## Minimal Node client (stdio)

```javascript
import { spawn } from "node:child_process";
import readline from "node:readline";

const proc = spawn("codex", ["app-server"]);
const rl = readline.createInterface({ input: proc.stdout });
const send = (m) => proc.stdin.write(JSON.stringify(m) + "\n");

rl.on("line", (line) => console.log("server:", JSON.parse(line)));

send({ method: "initialize", id: 0, params: { clientInfo: { name: "demo", title: "Demo", version: "0.0.1" } } });
send({ method: "initialized", params: {} });
send({ method: "thread/start", id: 1, params: { model: "gpt-5.6-sol" } });
// then turn/start with the returned thread id…
```

For most embedding needs, `mcp-server` is simpler and sufficient
([mcp-server.md](mcp-server.md)); reach here only when you need the extra
control.
