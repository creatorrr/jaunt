---
name: doctor
description: Use when a Python or TypeScript Jaunt workspace misbehaves, before a large build, or when asked to health-check setup. Reports freshness, TS unbuilt/invalid diagnostics, Node/npm/worker/compiler readiness, authentication, orphans, and duplicate hooks without building.
---

# Jaunt doctor

Locate this installed `SKILL.md`, resolve `../../scripts/doctor.sh` relative to
its directory, and run that absolute script path from the repository root.
`PLUGIN_ROOT` is available to plugin hooks but is not guaranteed in ordinary
skill-driven shell calls.

```bash
JAUNT_WORKSPACE_ROOT="$PWD" bash <absolute-plugin-root>/scripts/doctor.sh
```

For the current workspace alone, `jaunt doctor --json` provides the same
read-only core environment and status report through the installed CLI.

The report is read-only and makes no model calls. It checks:

- Codex availability and authentication.
- Jaunt, Python, Node, and npm availability.
- Every workspace's stale reasons, orphans, and TypeScript unbuilt, invalid,
  and diagnostic state, excluding nested Claude and Codex managed worktrees.
- Project-local `@usejaunt/ts` worker and supported TypeScript compiler setup.
- Hand-rolled Codex guards that duplicate the installed Codex plugin hook.

Use `clean --orphans` through the workspace runner for orphaned artifacts. For stale modules,
follow the build skill's taxonomy and preview likely model calls before
building. Run `codex login` when authentication is missing.
