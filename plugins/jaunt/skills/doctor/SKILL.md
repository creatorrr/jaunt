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
- The running Jaunt entrypoint, loaded module, Python executable, editable or
  installed distribution source, nearest `uv.lock`, and locked Jaunt version.
- Every workspace's stale reasons, orphans, and TypeScript unbuilt, invalid,
  and diagnostic state, excluding nested Claude and Codex managed worktrees.
- Project-local `@usejaunt/ts` worker and supported TypeScript compiler setup.
- Hand-rolled Codex guards that duplicate the installed Codex plugin hook.

Use `clean --orphans` through the workspace runner for orphaned artifacts. For stale modules,
follow the build skill's taxonomy and preview likely model calls before
building. Run `codex login` when authentication is missing.
Treat a running/locked Jaunt mismatch as actionable: a later `uv sync` can
replace the active source checkout. Do not infer the active implementation from
the lockfile alone; use `environment.jaunt.module` and `direct_url`. If
`distribution_matches_module` is false, report the metadata ambiguity instead
of attributing the loaded module to the first installed distribution.

Keep TypeScript resource failures distinct. A request timeout points to
`worker_timeout_seconds`; a startup timeout points to
`worker_startup_timeout_seconds`. A heap OOM is deterministic, is not replayed,
and points to `[target.ts].worker_heap_mb`. Do not describe any of these as a
missing worker/compiler unless the structured diagnostic says so. The plugin
status probe may time out and print an informational message, but its launcher
must still return success to the host.
