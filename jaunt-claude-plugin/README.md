# Jaunt Claude Code Plugin

Claude Code plugin for the [Jaunt](https://github.com/creatorrr/jaunt) spec-driven code generation framework.

## What's Included

### Skills

| Skill | Type | Description |
|-------|------|-------------|
| `jaunt` | Background knowledge | Always-on spec-writing guidance and Jaunt workflow context |
| `/jaunt-build` | Command | Generate implementations from `@jaunt.magic` specs |
| `/jaunt-test` | Command | Generate and run tests from `@jaunt.test` specs |
| `/jaunt-status` | Command (auto-invocable) | Check which modules are stale vs fresh |
| `/jaunt-init` | Command | Scaffold a new Jaunt project |
| `/jaunt-clean` | Command | Remove all `__generated__/` directories |

### Hooks

- **`__generated__/` file guard**: Blocks `Write`/`Edit` operations targeting generated files. Prevents accidental edits that would be overwritten on next build.
- **Bash guard**: Warns when bash commands would modify `__generated__/` directories.

### MCP Server

Exposes Jaunt tools (`build`, `test`, `status`, `clean`, `spec_info`) as MCP tools for programmatic use.

## Installation

### Local testing

```bash
claude --plugin-dir ./jaunt-claude-plugin
```

### From repository

```bash
claude /plugin install jaunt
```

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Jaunt installed (`uv pip install jaunt`)
- An LLM API key (OpenAI, Anthropic, or Cerebras) set as an environment variable

## Usage

Once installed, Claude Code will:

1. **Automatically** understand Jaunt concepts — specs, decorators, `jaunt.toml`, dependency graphs
2. **Help write specs** — Claude knows the principles for good `@jaunt.magic` and `@jaunt.test` stubs
3. **Protect generated files** — Hooks prevent accidental edits to `__generated__/` directories
4. **Provide slash commands** — Use `/jaunt-build`, `/jaunt-test`, `/jaunt-status`, `/jaunt-init`, `/jaunt-clean`

### Example Session

```
> /jaunt-init
  Creates jaunt.toml and project directories

> Help me write a spec for a URL shortener
  Claude writes @jaunt.magic spec stubs with detailed docstrings

> /jaunt-build
  Generates implementations via the configured LLM

> /jaunt-test
  Generates and runs tests

> /jaunt-status
  Shows which modules need rebuilding
```
