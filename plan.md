# Plan: Jaunt Claude Code Plugin

## Goal

Create a distributable Claude Code plugin for Jaunt that gives users a first-class spec-driven development experience inside Claude Code. The plugin packages skills, hooks, and MCP server integration so that any Jaunt project user can install it and get intelligent assistance for writing specs, building, testing, and iterating.

---

## What We're Building

A plugin directory at the repo root: `jaunt-claude-plugin/`

### Components

| Component | Type | Purpose |
|-----------|------|---------|
| `plugin.json` | Plugin manifest | Metadata, version, author info |
| `skills/jaunt/` | Skill (background) | Spec-writing guidance — always-on context for Claude |
| `skills/jaunt-build/` | Skill (user-invocable) | `/jaunt-build` — run `jaunt build` with options |
| `skills/jaunt-test/` | Skill (user-invocable) | `/jaunt-test` — run `jaunt test` with options |
| `skills/jaunt-status/` | Skill (auto-invocable) | `/jaunt-status` — check stale vs fresh modules |
| `skills/jaunt-init/` | Skill (user-invocable) | `/jaunt-init` — scaffold a new Jaunt project |
| `skills/jaunt-clean/` | Skill (user-invocable) | `/jaunt-clean` — remove `__generated__/` dirs |
| `hooks/hooks.json` | Hooks config | Lifecycle hooks for guardrails and DX |
| `scripts/guard-generated.sh` | Hook script | Block edits to `__generated__/` files |
| `.mcp.json` | MCP config | Wire up the existing `jaunt mcp serve` server |

---

## Step-by-Step Implementation

### Step 1: Create the plugin directory structure

```
jaunt-claude-plugin/
├── .claude-plugin/
│   └── plugin.json              # ONLY file here
├── skills/
│   ├── jaunt/
│   │   └── SKILL.md             # Background knowledge skill (spec-writing guide)
│   ├── jaunt-build/
│   │   └── SKILL.md             # /jaunt-build command
│   ├── jaunt-test/
│   │   └── SKILL.md             # /jaunt-test command
│   ├── jaunt-status/
│   │   └── SKILL.md             # /jaunt-status command
│   ├── jaunt-init/
│   │   └── SKILL.md             # /jaunt-init command
│   └── jaunt-clean/
│       └── SKILL.md             # /jaunt-clean command
├── hooks/
│   └── hooks.json               # Hook definitions
├── scripts/
│   └── guard-generated.sh       # PreToolUse hook script
├── .mcp.json                    # MCP server configuration
└── README.md                    # Plugin documentation
```

### Step 2: Write `plugin.json`

```json
{
  "name": "jaunt",
  "version": "0.2.0",
  "description": "Claude Code plugin for the Jaunt spec-driven code generation framework. Provides skills for writing specs, building, testing, and managing Jaunt projects.",
  "author": {
    "name": "Jaunt Contributors"
  },
  "repository": "https://github.com/creatorrr/jaunt",
  "license": "MIT",
  "keywords": ["jaunt", "spec-driven", "code-generation", "python", "llm"]
}
```

### Step 3: Write skill SKILL.md files

#### 3a. `skills/jaunt/SKILL.md` — Background knowledge skill

- **`user-invocable: false`** — always-on context, never manually triggered
- **`disable-model-invocation: false`** — Claude auto-loads this as background knowledge
- Content: Condensed version of the existing `.codex/skills/jaunt/SKILL.md` — spec-writing principles, decorator reference, anti-patterns, troubleshooting
- Keep under 500 lines; move detailed CLI reference and examples to `references/` subdirectory

#### 3b. `skills/jaunt-build/SKILL.md` — Build command

```yaml
---
name: jaunt-build
description: "Build Jaunt specs. Run jaunt build to generate implementations from @jaunt.magic stubs."
argument-hint: "[--force] [--target MODULE]"
disable-model-invocation: true    # Side effect: runs LLM generation
user-invocable: true
allowed-tools: Bash
---
```

- Runs `uv run jaunt build --json` with user-specified flags
- Parses JSON output and presents results (generated/skipped/failed modules)
- Supports `$ARGUMENTS` passthrough: `--force`, `--target MODULE`, `--jobs N`

#### 3c. `skills/jaunt-test/SKILL.md` — Test command

```yaml
---
name: jaunt-test
description: "Run Jaunt tests. Generate tests from @jaunt.test stubs and run pytest."
argument-hint: "[--force] [--no-build] [--no-run]"
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash
---
```

- Runs `uv run jaunt test --json` with flags
- Reports test pass/fail, generated test modules, pytest output

#### 3d. `skills/jaunt-status/SKILL.md` — Status command

```yaml
---
name: jaunt-status
description: "Check Jaunt module status. Show which spec modules are stale vs fresh."
disable-model-invocation: false   # Safe to auto-invoke
user-invocable: true
allowed-tools: Bash
---
```

- Runs `uv run jaunt status --json`
- Reports stale and fresh modules
- Safe for auto-invocation (read-only)

#### 3e. `skills/jaunt-init/SKILL.md` — Init command

```yaml
---
name: jaunt-init
description: "Initialize a new Jaunt project. Scaffold jaunt.toml, source, and test directories."
argument-hint: "[--force]"
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash
---
```

- Runs `uv run jaunt init`
- Reports created files

#### 3f. `skills/jaunt-clean/SKILL.md` — Clean command

```yaml
---
name: jaunt-clean
description: "Clean Jaunt generated files. Remove all __generated__ directories."
argument-hint: "[--dry-run]"
disable-model-invocation: true
user-invocable: true
allowed-tools: Bash
---
```

- Runs `uv run jaunt clean --json`
- Supports `--dry-run` to preview

### Step 4: Write hooks

#### `hooks/hooks.json`

Two hooks:

1. **Guard `__generated__/` files** — `PreToolUse` hook on `Write|Edit` tools
   - Matcher: `Write|Edit`
   - Type: `command`
   - Script: `scripts/guard-generated.sh`
   - Logic: Inspect the `file_path` parameter. If it contains `__generated__/`, block with a clear message: "Don't edit __generated__/ files directly — modify the spec and run `jaunt build` instead."
   - Timeout: 5000ms

2. **Warn on `__generated__/` deletion** — `PreToolUse` hook on `Bash`
   - Matcher: `Bash`
   - Type: `prompt`
   - Prompt: "If this command would delete or modify files inside a `__generated__/` directory, warn the user that these files are managed by Jaunt and will be regenerated. Suggest using `jaunt clean` instead."

#### `scripts/guard-generated.sh`

```bash
#!/usr/bin/env bash
# Reads tool input from stdin, checks if file_path contains __generated__/
input=$(cat)
file_path=$(echo "$input" | jq -r '.tool_input.file_path // empty')
if [[ "$file_path" == *"__generated__/"* ]]; then
  echo '{"decision":"block","reason":"Do not edit __generated__/ files directly. Modify the spec stub and run jaunt build (or /jaunt-build) to regenerate."}'
else
  echo '{"decision":"approve"}'
fi
```

### Step 5: Configure MCP server (`.mcp.json`)

Wire up the existing `jaunt mcp serve` command:

```json
{
  "mcpServers": {
    "jaunt": {
      "command": "uv",
      "args": ["run", "jaunt", "mcp", "serve"],
      "description": "Jaunt spec-driven code generation tools"
    }
  }
}
```

This exposes `jaunt_build`, `jaunt_test`, `jaunt_status`, `jaunt_spec_info`, and `jaunt_clean` as MCP tools — complementing the slash-command skills.

### Step 6: Write README.md

Brief plugin documentation covering:
- What the plugin provides (skills, hooks, MCP)
- Installation: `claude /plugin install jaunt` or `claude --plugin-dir ./jaunt-claude-plugin`
- Available commands (`/jaunt-build`, `/jaunt-test`, `/jaunt-status`, `/jaunt-init`, `/jaunt-clean`)
- Hook behavior (generated file protection)
- Requirements (Python 3.12+, `uv`, Jaunt installed)

### Step 7: Test locally

- Run `claude --plugin-dir ./jaunt-claude-plugin` to verify all skills load
- Test each `/jaunt-*` command
- Test the `__generated__/` guard hook by attempting to edit a generated file
- Verify MCP tools appear and function

---

## Design Decisions

1. **Skills vs MCP tools**: Both are provided. Skills give users explicit `/jaunt-build` commands with clear UX. MCP tools let Claude use Jaunt programmatically when appropriate (e.g., auto-checking status). They complement each other.

2. **Background knowledge skill** (`user-invocable: false`): The spec-writing guide is always loaded into Claude's context so it can help write good specs without the user needing to invoke anything.

3. **`disable-model-invocation: true` for build/test/init/clean**: These have side effects (LLM calls cost money, file creation/deletion). Only the user should trigger them explicitly.

4. **`disable-model-invocation: false` for status**: Read-only and safe — Claude can auto-check status when relevant.

5. **Hook for `__generated__/` protection**: This is the single most important guardrail. Editing generated files is a top Jaunt anti-pattern, and a blocking `PreToolUse` hook prevents it automatically.

6. **Plugin at repo root, not inside `src/`**: The plugin is a distributable artifact, not part of the Jaunt Python package. It lives at `jaunt-claude-plugin/` at the repo root.

---

## Files to Create (Summary)

| # | File | Lines (est.) |
|---|------|-------------|
| 1 | `jaunt-claude-plugin/.claude-plugin/plugin.json` | ~12 |
| 2 | `jaunt-claude-plugin/skills/jaunt/SKILL.md` | ~200 |
| 3 | `jaunt-claude-plugin/skills/jaunt/references/cli.md` | ~80 |
| 4 | `jaunt-claude-plugin/skills/jaunt/references/examples.md` | ~100 |
| 5 | `jaunt-claude-plugin/skills/jaunt-build/SKILL.md` | ~40 |
| 6 | `jaunt-claude-plugin/skills/jaunt-test/SKILL.md` | ~45 |
| 7 | `jaunt-claude-plugin/skills/jaunt-status/SKILL.md` | ~30 |
| 8 | `jaunt-claude-plugin/skills/jaunt-init/SKILL.md` | ~35 |
| 9 | `jaunt-claude-plugin/skills/jaunt-clean/SKILL.md` | ~30 |
| 10 | `jaunt-claude-plugin/hooks/hooks.json` | ~30 |
| 11 | `jaunt-claude-plugin/scripts/guard-generated.sh` | ~15 |
| 12 | `jaunt-claude-plugin/.mcp.json` | ~10 |
| 13 | `jaunt-claude-plugin/README.md` | ~60 |
