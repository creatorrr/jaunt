---
name: doctor
description: Use when a Jaunt workspace misbehaves, before a large build, or when asked to health-check setup. Reports environment, workspace freshness reasons, orphans, authentication, and duplicate Claude/Codex hooks without building.
---

# Jaunt doctor

Locate this installed `SKILL.md`, resolve `../../scripts/doctor.sh` relative to
its directory, and run that absolute script path from the repository root.
`PLUGIN_ROOT` is available to plugin hooks but is not guaranteed in ordinary
skill-driven shell calls.

```bash
JAUNT_WORKSPACE_ROOT="$PWD" bash <absolute-plugin-root>/scripts/doctor.sh
```

The report is read-only and makes no model calls. It checks:

- Codex availability and authentication.
- Jaunt and Python availability.
- Every discovered workspace's current stale reasons and orphans.
- Hand-rolled Claude or Codex guards that duplicate the installed plugin hook.

Use `uv run jaunt clean --orphans` for orphaned artifacts. For stale modules,
follow the build skill's taxonomy and preview likely model calls before
building. Run `codex login` when authentication is missing.
