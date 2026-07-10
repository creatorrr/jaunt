# Codex Config, Sandbox & Auth (cross-cutting)

Model selection, config overrides, sandboxing, features, and authentication work
the same across `exec`, `mcp-server`, `app-server`, and the Agents SDK. This is
the shared layer.

## CODEX_HOME

Codex reads everything from `$CODEX_HOME` (default `~/.codex`):

```
~/.codex/
  config.toml          # base configuration
  <name>.config.toml   # named profiles (layered with -p/--profile)
  auth.json            # credentials (ChatGPT login)  ← treat as a password
```

`--ignore-user-config` skips `config.toml` (auth is still read). Setting a
different `CODEX_HOME` lets you run isolated configs/identities side by side.

## Authentication

Two ways:

1. **ChatGPT login** → writes `~/.codex/auth.json`.
   ```bash
   codex login          # interactive
   codex logout
   ```
2. **API key** for a single invocation:
   ```bash
   export CODEX_API_KEY=sk-...
   codex exec "…"
   ```

`codex doctor` diagnoses auth/config/runtime issues. **Never** commit `auth.json`
or expose `CODEX_API_KEY` to a CI step that runs untrusted code — prefer the
Codex GitHub Action's credential proxy.

## config.toml + overrides

`config.toml` holds defaults (model, sandbox, providers, MCP servers, features).
Override any value per-invocation with `-c key=value` — the value is **parsed as
TOML**, so quote strings and use dotted paths for nesting:

```bash
codex exec -c model="gpt-5.6-sol" \
           -c model_reasoning_effort="medium" \
           -c 'sandbox_permissions=["disk-full-read-access"]' \
           -c shell_environment_policy.inherit=all \
           "…"
```

Commonly-set keys:

| Key | Meaning |
|-----|---------|
| `model` | Default model (`gpt-5.6-sol`, …). |
| `model_reasoning_effort` | `low` \| `medium` \| `high` — reasoning budget. The user's automation uses `medium`. |
| `model_provider` | Provider id (for non-OpenAI / OSS backends). |
| `approval_policy` | Default of `-a/--ask-for-approval`. |
| `sandbox_mode` | Default sandbox policy. |
| `shell_environment_policy.inherit` | What env the sandboxed shell inherits (`all`, `core`, …). |
| `features.<name>` | Feature flags (see below). |
| `mcp_servers.<name>` | External MCP servers Codex should connect to (managed via `codex mcp add`). |

In `mcp-server` / `app-server`, the same overrides go through the `config` object
param (mcp-server) or `config/value/write` (app-server) instead of `-c`.

### Profiles

`-p, --profile <NAME>` layers `$CODEX_HOME/<NAME>.config.toml` on top of the base
config — keep a `ci.config.toml` (e.g. `approval_policy = "never"`, sandbox
workspace-write) and select it with `-p ci`.

## Features

`--enable <FEATURE>` / `--disable <FEATURE>` is sugar for
`-c features.<FEATURE>=true|false`. Features the user runs:

```bash
codex --enable multi_agent --enable collaboration_modes
```

Inspect what your binary supports: `codex features`.

## Sandbox & approvals

Two independent controls — what Codex *can* touch, and when it *asks*.

**Sandbox** (`-s/--sandbox`, or `sandbox:` param):

| Mode | Effect |
|------|--------|
| `read-only` | **Default.** Codex can read but not write/execute outside reads. |
| `workspace-write` | Read + write within the workspace (and `--add-dir` paths). **Needed for code generation.** |
| `danger-full-access` | No sandbox restrictions. |

`-C/--cd` sets the workspace root; `--add-dir` adds extra writable dirs;
`--skip-git-repo-check` allows running outside a git repo.

**Approval** (`-a/--ask-for-approval`, or `approval-policy:` param):

| Policy | When Codex asks a human |
|--------|--------------------------|
| `untrusted` | Runs only trusted commands silently; escalates anything else. |
| `on-request` | Model decides when to ask. Good for interactive use. |
| `never` | Never asks; failures return to the model. **Use for headless runs.** |
| `on-failure` | *Deprecated* — prefer `on-request` (interactive) or `never` (headless). |

**The nuclear option:** `--dangerously-bypass-approvals-and-sandbox` skips both —
no approvals, no sandbox. Only inside an environment that is *already* externally
sandboxed. See the allow-rule gotcha in [exec.md](exec.md) when invoking Codex
from inside another agent harness.

## Local / OSS providers

```bash
codex exec --oss --local-provider ollama -m "qwen2.5-coder" "…"
```

`--oss` routes to a local provider; `--local-provider lmstudio|ollama` picks
which. Useful for offline or cost-free iteration.
