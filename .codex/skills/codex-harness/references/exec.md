# `codex exec` — Non-Interactive Codex

`codex exec` (alias `codex e`) runs Codex from scripts and CI with no TUI. You
hand it a prompt and a workspace; it works autonomously and exits. This is the
simplest way to drive Codex and the basis of the `cdx` alias.

## The shape of a run

```bash
# Prompt as an argument
codex exec "add type hints to src/jaunt/digest.py"

# Prompt from stdin (use `-` or just pipe). Avoids shell-quoting hell:
codex exec - < prompt.txt
echo "summarize the failing tests" | codex exec -

# Prompt arg + piped stdin → stdin is appended as a <stdin> block (context):
npm test 2>&1 | codex exec "explain why these tests fail and fix them"
```

If a prompt is given as an argument *and* stdin is piped, stdin is appended to
the prompt inside a `<stdin>` block — handy for feeding logs/diffs as context.

## Flags that matter

### Output / structured results
| Flag | Effect |
|------|--------|
| `--json` | Stream every event to stdout as JSONL (one JSON object per line). |
| `-o, --output-last-message <FILE>` | Write the agent's final message to `FILE`. |
| `--output-schema <FILE>` | A JSON Schema the model's **final response** must conform to. Gives you stable, validated fields. |
| `--color <always\|never\|auto>` | Color control for human-facing logs. |

### Workspace / sandbox / approvals
| Flag | Effect |
|------|--------|
| `-C, --cd <DIR>` | Use `DIR` as the working root. |
| `--add-dir <DIR>` | Extra writable directory alongside the workspace. |
| `-s, --sandbox <read-only\|workspace-write\|danger-full-access>` | Sandbox policy. **Default `read-only`** — pass `workspace-write` to let Codex edit files. |
| `-a, --ask-for-approval <untrusted\|on-request\|never>` | When the model must ask before running a command. For non-interactive runs use `never` (`on-failure` is deprecated). |
| `--skip-git-repo-check` | Allow running outside a Git repo. |
| `--dangerously-bypass-approvals-and-sandbox` | Skip *all* approvals and run unsandboxed. Only inside an already-sandboxed environment. |

### Model / config
| Flag | Effect |
|------|--------|
| `-m, --model <MODEL>` | Model override (e.g. `gpt-5.5`). |
| `-c, --config <key=value>` | Override any `config.toml` value (TOML-parsed; dotted paths). Repeatable. |
| `-p, --profile <NAME>` | Layer `$CODEX_HOME/<NAME>.config.toml` on the base config. |
| `--enable <FEATURE>` / `--disable <FEATURE>` | Toggle a feature (e.g. `multi_agent`). |
| `--oss` / `--local-provider <lmstudio\|ollama>` | Use a local open-source provider. |
| `--ephemeral` | Don't persist a session rollout file to disk. |
| `--ignore-user-config` | Skip `$CODEX_HOME/config.toml` (auth still honored). |
| `--ignore-rules` | Skip `.rules` execpolicy files. |
| `-i, --image <FILE>...` | Attach image(s) to the initial prompt. |

See [config-and-auth.md](config-and-auth.md) for `-c`/`-p`/features/sandbox detail.

## The `--json` event stream

With `--json`, stdout is JSONL. Event `type`s use **dotted** names (contrast the
app-server's slashed `thread/start`):

```
thread.started        # a conversation/session began (carries the session id)
turn.started          # the agent began a unit of work
item.started          # an item within the turn began
item.completed        # an item finished (see item kinds below)
turn.completed        # the turn finished successfully
turn.failed           # the turn failed (carries error detail)
error                 # a top-level error
```

**Item kinds** carried by `item.*` events: agent messages, reasoning, command
executions, file changes (patches), MCP tool calls, web searches, and plan
updates. To know "what did Codex change," watch `item.completed` for file-change
items, or just diff the workspace afterward.

Minimal consumer:

```python
import json, subprocess

proc = subprocess.Popen(
    ["codex", "exec", "--json", "--sandbox", "workspace-write",
     "-a", "never", "-C", "/path/to/repo", "implement the TODOs in foo.py"],
    stdout=subprocess.PIPE, text=True,
)
final = None
for line in proc.stdout:
    evt = json.loads(line)
    if evt.get("type") == "item.completed":
        ...  # inspect file changes / messages
    elif evt.get("type") == "turn.failed":
        raise RuntimeError(evt)
    elif evt.get("type") == "turn.completed":
        final = evt
proc.wait()
```

> The exact JSON envelope (top-level keys, nesting under `item`) can shift
> between versions. Run one `codex exec --json "hi"` and inspect real lines
> before committing a parser. For a *guaranteed* result shape, prefer
> `--output-schema` + `--output-last-message` over scraping the stream.

### Structured final answer with `--output-schema`

```bash
cat > /tmp/schema.json <<'JSON'
{ "type": "object",
  "properties": {
    "summary": {"type": "string"},
    "files_changed": {"type": "array", "items": {"type": "string"}},
    "tests_pass": {"type": "boolean"}
  },
  "required": ["summary", "tests_pass"], "additionalProperties": false }
JSON

codex exec --output-schema /tmp/schema.json -o /tmp/result.json \
  --sandbox workspace-write -a never \
  "implement the function and run pytest; report what changed"
# /tmp/result.json now holds the final message, conforming to the schema.
```

## Sessions: resume / review

```bash
# Resume a prior run and add a follow-up (two-stage pipelines):
codex exec resume --last "now add tests for what you just wrote"
codex exec resume <SESSION_ID> "address the review comments"   # UUID or thread name
codex exec resume --all                                        # list across cwds

# Non-interactive code review (also: top-level `codex review`):
codex exec review                       # or: codex review --uncommitted
codex review --base main                # review vs a base branch
codex review --commit <SHA>
```

`SESSION_ID` accepts a UUID or a thread name (UUID wins if it parses). `--last`
picks the newest. Sessions persist by default; `--ephemeral` opts out.

## Exit status & errors

`codex exec` exits `0` on success and non-zero on failure. For programmatic
control, **don't rely on exit codes alone**. Jaunt's `CodexBackend` treats a
real exec failure as any of:

- the JSONL stream contains a `turn.failed` event;
- the JSONL stream contains a top-level `error` event;
- the subprocess return code is non-zero;
- the stream never emits `turn.completed` (protocol failure).

The backend raises `JauntGenerationError` with Codex stderr included (truncated)
so the builder reports the real failure. A target file that is unchanged from
the seed is **not** an exec failure: a completed turn may legitimately write
identical or low-quality content, and that remains a validation concern for
`generate_with_retry` / `validate_generated_source`.

Outside Jaunt, use the same shape: parse `--json` for `turn.failed` / `error`
events, require `turn.completed`, check the return code, or use
`--output-last-message` for a final message. Wrap runs in a timeout; an agent can
loop.

## CI / automation guidance

- Use the **Codex GitHub Action** in GitHub workflows — it proxies credentials
  instead of exposing `CODEX_API_KEY` to job steps that check out code.
- **Never** set `CODEX_API_KEY` as a job-level env var in a workflow that runs
  untrusted/checked-out code. Treat `~/.codex/auth.json` as a secret.
- For deterministic unattended runs: `-a never -s workspace-write
  --skip-git-repo-check` (add `--ephemeral` if you don't want rollout files).
- Bound it: run under `timeout`, capture `--json` to a log, fail the job on a
  `turn.failed` event.

## The `cdx` alias and the bypass allow-rule

The user's interactive alias:

```
cdx = codex --search --dangerously-bypass-approvals-and-sandbox \
            --enable multi_agent --enable collaboration_modes
```

The alias is **not** loaded in non-interactive subagent shells. Inside automation
(e.g. a background workflow), call the binary explicitly and feed the prompt via
stdin to dodge quoting:

```bash
codex exec --dangerously-bypass-approvals-and-sandbox \
  -c model_reasoning_effort="high" - < promptfile
```

**Permission gotcha when Codex runs inside Claude Code:** the auto-mode
classifier blocks the bypass invocation *and* blocks an agent from adding its own
allow-rule (self-modification). A **human** must add
`"Bash(codex exec --dangerously-bypass-approvals-and-sandbox:*)"` to
`.claude/settings.local.json` under `permissions.allow`. Once present,
background workflow subagents can run it unattended.
