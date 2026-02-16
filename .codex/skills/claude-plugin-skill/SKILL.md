---
name: claude-plugin-skill
description: Build Claude Code plugins, skills, hooks, and subagents. Use when creating custom slash commands, writing SKILL.md files, designing plugin architectures, configuring hooks, setting up marketplaces, or extending Claude Code. Trigger for requests mentioning Claude Code plugin, skill, slash command, custom command, SKILL.md, hooks, subagent, or Claude Code extensibility.
---

# Claude Code Plugin & Skill Development

## Overview

Claude Code has a layered extensibility system. From smallest to largest:

| Unit | What it is | Where it lives |
|------|-----------|----------------|
| **Skill** | A `SKILL.md` file with instructions Claude loads into context | `.claude/skills/<name>/SKILL.md` |
| **Subagent** | An isolated agent that runs in its own context window | `.claude/skills/<name>/SKILL.md` with `context: fork` |
| **Plugin** | A packaged collection of skills, hooks, MCP servers, and subagents | `<plugin-dir>/.claude-plugin/plugin.json` |
| **Marketplace** | A catalog of plugins for distribution | `<marketplace-dir>/.claude-plugin/marketplace.json` |

Skills are the atomic building block. Start here.

## Skills

### File format

Every skill is a directory containing a `SKILL.md` file with YAML frontmatter and Markdown body:

```
.claude/skills/my-skill/
  SKILL.md           # Required — instructions
  references/        # Optional — detailed docs Claude can read
  scripts/           # Optional — scripts Claude can execute
  templates/         # Optional — fill-in templates
```

### SKILL.md structure

```yaml
---
name: my-skill
description: What this skill does and when Claude should use it
argument-hint: "[arg]"
disable-model-invocation: false
user-invocable: true
allowed-tools: Read, Grep, Glob
model: sonnet
context: fork
agent: Explore
---

Your instructions in Markdown here.
Use $ARGUMENTS for all arguments passed by the user.
Use $ARGUMENTS[0], $ARGUMENTS[1] for specific positional args.
```

### Frontmatter fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | No | directory name | Lowercase, hyphens, max 64 chars |
| `description` | Recommended | — | When/how to use. Claude reads this for auto-invocation. Max 1024 chars |
| `argument-hint` | No | — | Shown in autocomplete, e.g. `[issue-number]` |
| `disable-model-invocation` | No | `false` | `true` = only user can invoke (not Claude) |
| `user-invocable` | No | `true` | `false` = hidden from `/` menu, only Claude invokes |
| `allowed-tools` | No | — | Tools allowed without permission prompts |
| `model` | No | — | Model override when skill is active |
| `context` | No | — | `fork` = run in isolated subagent context |
| `agent` | No | — | Subagent type: `Explore`, `Plan`, `general-purpose` |
| `hooks` | No | — | Lifecycle hooks scoped to this skill |

### Scope and discovery

Skills are discovered automatically at three levels:

| Scope | Path | Visibility |
|-------|------|-----------|
| Personal | `~/.claude/skills/<name>/SKILL.md` | All your projects |
| Project | `.claude/skills/<name>/SKILL.md` | Current project |
| Enterprise | Managed settings | All org users |

Priority: enterprise > personal > project.

### Invocation modes

| Configuration | User invokes | Claude invokes | Context behavior |
|---------------|-------------|---------------|-----------------|
| Default (both true) | `/skill-name` | Automatic | Description always in context; full content loads on invoke |
| `disable-model-invocation: true` | `/skill-name` | Never | Description NOT in context |
| `user-invocable: false` | Never | Automatic | Description always in context |

### String substitutions

| Variable | Resolves to |
|----------|-------------|
| `$ARGUMENTS` | All arguments the user passed |
| `$ARGUMENTS[N]` | Specific argument (0-indexed) |
| `$N` | Shorthand for `$ARGUMENTS[N]` |
| `${CLAUDE_SESSION_ID}` | Current session ID |

### Dynamic context injection

The `` !`command` `` syntax runs shell commands as preprocessing before the skill body is sent to Claude:

```yaml
---
name: pr-review
description: Review a pull request
context: fork
agent: Explore
allowed-tools: Bash(gh *)
---

## Context
- Diff: !`gh pr diff $ARGUMENTS`
- Comments: !`gh pr view $ARGUMENTS --comments`

Review this PR for bugs, security issues, and style.
```

The shell output replaces the `` !`...` `` inline.

## Subagents

A subagent is a skill with `context: fork`. It runs in an isolated context window, does its work, and returns a summary to the main conversation.

```yaml
---
name: deep-research
description: Research a topic by exploring the codebase
context: fork
agent: Explore
---

Research $ARGUMENTS thoroughly:
1. Find relevant files using Glob and Grep
2. Read and analyze the code
3. Trace call chains and data flow
4. Return findings with file:line references
```

**When to use subagents:**
- Research/analysis tasks that need deep exploration
- Tasks that would bloat the main conversation context
- Isolated operations that should not see (or pollute) the main thread

**Subagent types:**

| Agent | Best for |
|-------|---------|
| `Explore` | Codebase search, reading files, answering questions |
| `Plan` | Designing implementation strategies |
| `general-purpose` | Multi-step tasks with all tools |

**Gotcha:** Subagents cannot spawn subagents. No nesting.

## Plugins

A plugin packages skills, hooks, MCP servers, and subagents for distribution.

### Directory layout

```
my-plugin/
  .claude-plugin/
    plugin.json          # Manifest (ONLY file in this dir)
  skills/                # Agent Skills directories
    code-review/
      SKILL.md
  commands/              # Legacy slash commands (.md files)
  agents/                # Subagent definitions
  hooks/
    hooks.json           # Event handlers
  .mcp.json              # MCP server configs
  .lsp.json              # LSP server configs
  scripts/               # Utility scripts
  README.md
```

**Critical rule:** Never put `skills/`, `commands/`, `agents/`, or `hooks/` inside `.claude-plugin/`. Only `plugin.json` goes there.

### plugin.json manifest

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "What this plugin does",
  "author": {
    "name": "Author Name",
    "email": "author@example.com",
    "url": "https://github.com/author"
  },
  "homepage": "https://docs.example.com/plugin",
  "repository": "https://github.com/author/plugin",
  "license": "MIT",
  "keywords": ["keyword1", "keyword2"]
}
```

Only `name` is technically required. The manifest itself is optional — Claude Code auto-discovers components in default locations.

The `name` field doubles as a namespace prefix. A skill named `review` in plugin `my-plugin` becomes `/my-plugin:review`.

### Path references in plugins

Always use `${CLAUDE_PLUGIN_ROOT}` for paths in hooks and scripts — plugins are copied to a cache directory on install, so relative paths like `../shared/` will break.

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Write|Edit",
      "hooks": [{
        "type": "command",
        "command": "${CLAUDE_PLUGIN_ROOT}/scripts/format.sh"
      }]
    }]
  }
}
```

### Installing plugins

```bash
# From a marketplace
claude plugin install my-plugin@marketplace-name

# Local development/testing
claude --plugin-dir ./my-plugin

# Interactive
/plugin install my-plugin@marketplace-name
```

**Installation scopes:**

| Scope | File | Use case |
|-------|------|---------|
| `user` | `~/.claude/settings.json` | Personal, all projects (default) |
| `project` | `.claude/settings.json` | Team, version controlled |
| `local` | `.claude/settings.local.json` | Project-specific, gitignored |
| `managed` | `managed-settings.json` | Org-wide, read-only |

## Hooks

Hooks are event-driven automation that fire at lifecycle points.

### Hook types

| Type | What it does |
|------|-------------|
| `command` | Run a shell script |
| `prompt` | Single-turn LLM evaluation |
| `agent` | Multi-turn subagent verification |

### Available events

| Event | When | Can block? |
|-------|------|-----------|
| `SessionStart` | Session begins/resumes | No |
| `UserPromptSubmit` | User submits prompt | Yes |
| `PreToolUse` | Before tool call | Yes |
| `PermissionRequest` | Permission dialog shown | Yes |
| `PostToolUse` | After tool succeeds | No |
| `PostToolUseFailure` | After tool fails | No |
| `Stop` | Claude finishes responding | Yes |
| `SubagentStart` | Subagent spawned | No |
| `SubagentStop` | Subagent finishes | Yes |
| `Notification` | Notification sent | No |
| `SessionEnd` | Session terminates | No |

### Hook configuration

In `.claude/settings.json` or a plugin's `hooks/hooks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "./scripts/validate-command.sh",
            "timeout": 10
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/scripts/lint.sh"
          }
        ]
      }
    ]
  }
}
```

**Blocking hooks** return JSON with a `decision` field:

```json
{"decision": "block", "reason": "Command not allowed by policy"}
```

```json
{"decision": "approve"}
```

**Non-blocking hooks** (PostToolUse, SessionStart, etc.) run in the background. Their `decision` field is ignored.

### Hook gotchas

1. Hook stdout must be ONLY the JSON object — shell profile output will interfere.
2. Hooks are snapshotted at session start. Changes require `/hooks` review.
3. Always `chmod +x` your hook scripts.
4. Async hooks cannot block — the action already proceeded.

## Marketplaces

A marketplace is a catalog of plugins for distribution.

### Structure

```
my-marketplace/
  .claude-plugin/
    marketplace.json
  plugins/
    plugin-a/
      .claude-plugin/
        plugin.json
      skills/
        ...
    plugin-b/
      ...
```

### marketplace.json

```json
{
  "name": "team-tools",
  "owner": {"name": "DevTools Team"},
  "plugins": [
    {
      "name": "code-formatter",
      "source": "./plugins/formatter",
      "description": "Auto code formatting",
      "version": "2.1.0"
    },
    {
      "name": "github-plugin",
      "source": {"source": "github", "repo": "owner/repo"}
    }
  ]
}
```

**Plugin sources:** relative paths, GitHub repos, git URLs, npm, pip.

### Marketplace commands

```bash
/plugin marketplace add owner/repo           # Add GitHub marketplace
/plugin marketplace add https://git.example.com/tools.git  # Git URL
/plugin install my-plugin@marketplace-name   # Install from marketplace
/plugin marketplace update                   # Update catalog
```

## Complete Examples

### Minimal skill — coding conventions

```yaml
---
name: api-conventions
description: API design patterns for this project. Use when writing or reviewing API endpoints.
---

When writing API endpoints:
- Use RESTful naming (plural nouns for collections)
- Return `{"error": {"code": "...", "message": "..."}}` for errors
- Include pagination for list endpoints (default limit=20, max=100)
- Validate all inputs with descriptive error messages
```

### Task skill with arguments

```yaml
---
name: fix-issue
description: Fix a GitHub issue by number
disable-model-invocation: true
argument-hint: "[issue-number]"
---

Fix GitHub issue #$ARGUMENTS following our coding standards.

1. Fetch the issue: !`gh issue view $ARGUMENTS`
2. Read the description and understand requirements
3. Find relevant code using Grep and Glob
4. Implement the fix
5. Write tests
6. Create a commit with message "fix: resolve #$ARGUMENTS"
```

### Forked research subagent

```yaml
---
name: architecture-review
description: Review architecture of a module or subsystem
context: fork
agent: Explore
---

Analyze the architecture of $ARGUMENTS:
1. Map the module structure and key files
2. Identify public API surface
3. Trace data flow and dependencies
4. Note any architectural concerns (coupling, complexity, missing abstractions)
5. Return findings with specific file:line references
```

### Full plugin with hooks

**`.claude-plugin/plugin.json`:**
```json
{
  "name": "quality-gates",
  "version": "1.0.0",
  "description": "Automated quality checks on code changes",
  "author": {"name": "DevTools Team"}
}
```

**`skills/review/SKILL.md`:**
```yaml
---
name: review
description: Review code changes for bugs and security issues
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash(git diff:*)
---

Review the staged code changes:

1. **Bugs**: logic errors, off-by-one, null references
2. **Security**: injection, XSS, exposed secrets, path traversal
3. **Performance**: N+1 queries, unnecessary allocations

Classify findings: CRITICAL (must fix), WARNING (should fix), SUGGESTION.
```

**`hooks/hooks.json`:**
```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/scripts/lint.sh",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

**`scripts/lint.sh`:**
```bash
#!/usr/bin/env bash
set -euo pipefail
ruff check --fix "${TOOL_INPUT_file_path:-}" 2>/dev/null || true
```

## Design Best Practices

### Skill design

1. **Keep `SKILL.md` under 500 lines.** Move detailed reference material into `references/` files.
2. **Write specific descriptions** with keywords users would naturally say — Claude uses the description to decide when to auto-invoke.
3. **Use `disable-model-invocation: true`** for skills with side effects (deploy, commit, send messages).
4. **Use `user-invocable: false`** for background knowledge that Claude should always have but users would never invoke directly.
5. **Use `context: fork`** for research, analysis, and any task that would bloat main context.
6. **Restrict tools** with `allowed-tools` for safety-sensitive skills.
7. **Start standalone, convert to plugin later** — iterate in `.claude/skills/` before packaging.
8. **Version control project skills** — commit `.claude/skills/` so teammates get them.

### Plugin design

1. **Only `plugin.json` in `.claude-plugin/`** — put everything else at the plugin root.
2. **Use `${CLAUDE_PLUGIN_ROOT}`** for all paths in hooks, MCP configs, and scripts.
3. **Semantic versioning** — MAJOR.MINOR.PATCH.
4. **Test locally first** with `claude --plugin-dir ./my-plugin`.
5. **Namespace awareness** — plugins prefix skill names. `review` in plugin `qa` becomes `/qa:review`.

### Hook design

1. **Hook stdout = JSON only.** Shell profile noise will break parsing.
2. **Set timeouts.** Long-running hooks block the session.
3. **`chmod +x` all scripts.**
4. **Use matchers** to scope hooks to specific tools (e.g., `Write|Edit` not `*`).

## Common Gotchas

1. **Context budget overflow:** Too many skills exceed the 2% context window budget. Check with `/context`. Reduce by using `disable-model-invocation: true` on rarely-needed skills.
2. **`context: fork` without a task:** If your skill only has guidelines and no actionable task, the subagent returns without meaningful output.
3. **Path traversal in plugins:** Plugins are copied to a cache on install. Paths like `../shared-utils` break. Use `${CLAUDE_PLUGIN_ROOT}`.
4. **Skill name conflicts:** Plugin skills are namespaced (`plugin:skill`). Standalone skills are not. If two standalone skills share a name, scope priority applies (enterprise > personal > project).
5. **Hooks snapshotted at session start:** Editing hook files mid-session has no effect until reviewed via `/hooks`.
6. **Subagents cannot nest:** A subagent cannot spawn another subagent.
7. **`allowed-tools` syntax:** Tool names are case-sensitive. Use `Bash(git *)` for pattern matching, not `bash(git *)`.
8. **Legacy commands still work:** `.claude/commands/*.md` files are auto-migrated to skills. No action needed, but new work should use the skills format.

## Migrating from Legacy Commands

Old `.claude/commands/review.md` files still work. To migrate:

1. Create `.claude/skills/review/SKILL.md`
2. Add YAML frontmatter with `name` and `description`
3. Move the markdown body into the skill
4. Delete the old `.claude/commands/review.md`

The skill format adds: auto-invocation, `context: fork`, `allowed-tools`, hooks, and supporting files.

## Reference

See `references/checklists.md` for step-by-step checklists for building skills and plugins.
