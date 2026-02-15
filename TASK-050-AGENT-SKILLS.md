---
id: "050"
title: Agent Skills & CLAUDE.md
status: done
priority: 3
effort: small
depends: ["030"]
---

# TASK-050: Agent Skills & CLAUDE.md

## Problem

No `CLAUDE.md` for Claude Code. The skill export command wasn't implemented.
Agents had no structured guidance for working with jaunt.

## Solution (Implemented)

- Created `CLAUDE.md` with:
  - Quick reference commands (install, test, lint, typecheck, build)
  - Project layout overview
  - Key concepts (specs, generated dir, incremental builds, dependency graph)
  - Configuration reference (`jaunt.toml`)
  - Exit codes table
  - JSON output mode documentation
  - Testing and lint instructions

## What Remains (Future)

- Implement `jaunt skill export` CLI command:
  ```bash
  jaunt skill export                    # write .agents/skills/jaunt/
  jaunt skill export --format claude    # write CLAUDE.md to project root
  jaunt skill export --format cursor    # write .cursorrules
  ```
- Make exported skills version-aware (reference installed Jaunt version and
  configured LLM provider)
