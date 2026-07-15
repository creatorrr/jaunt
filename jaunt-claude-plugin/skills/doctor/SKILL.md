---
name: doctor
description: Use when a Python or TypeScript Jaunt workspace misbehaves, before a large build, or when asked to health-check setup. Reports freshness, TS unbuilt/invalid diagnostics, Node/npm/worker/compiler readiness, authentication, orphans, and duplicate hooks without building.
---

# Jaunt doctor

Run from the repository root:

```bash
JAUNT_WORKSPACE_ROOT="$PWD" bash "${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}/scripts/doctor.sh"
```

For the current workspace alone, `jaunt doctor --json` provides the same
read-only core environment and status report through the installed CLI.

The report is read-only and makes no model calls. It checks:

- Codex availability and authentication.
- Jaunt, Python, Node, and npm availability.
- Every workspace's stale reasons, orphans, and TypeScript unbuilt, invalid,
  and diagnostic state, excluding nested Claude and Codex managed worktrees.
- Project-local `@usejaunt/ts` worker and supported TypeScript compiler setup.
- Hand-rolled Claude guards that duplicate the installed Claude plugin hook.

Use `clean --orphans` through the workspace runner for orphaned artifacts. For stale modules,
follow the build skill's taxonomy and preview likely model calls before
building. Run `codex login` when authentication is missing.

Keep TypeScript resource failures distinct. A request timeout points to
`worker_timeout_seconds`; a startup timeout points to
`worker_startup_timeout_seconds`. A heap OOM is deterministic, is not replayed,
and points to `[target.ts].worker_heap_mb`. Do not describe any of these as a
missing worker/compiler unless the structured diagnostic says so. The plugin
status probe may time out and print an informational message, but its launcher
must still return success to the host.
