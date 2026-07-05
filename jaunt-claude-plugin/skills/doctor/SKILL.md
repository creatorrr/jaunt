---
name: doctor
description: Use when a jaunt project misbehaves, before a big build, or when asked to health-check jaunt setup — checks codex auth, per-project freshness/orphans, cross-project config drift, and duplicate guard hooks. Free and deterministic (no model calls, no builds).
---

# /jaunt:doctor — health check

Doctor is free and deterministic — it never spends money; it reports what a build WOULD bill.

## Run it

Run from the repo root. It uses `$CLAUDE_PROJECT_DIR` and falls back to `$PWD`.

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh"
```

## Reading the report

- `== environment` checks Codex CLI availability, Codex auth, Jaunt availability through `uv`, and `python3`.
- `== projects` lists discovered Jaunt projects, freshness, stale classes, and orphans.
- `== config drift` compares `[codex]` and `build.instructions` across projects.
- `== duplicate guard hooks` flags a hand-rolled `jaunt guard` hook that would double-run beside the plugin hook.

## Fix table

| Finding | Meaning | Fix |
|---|---|---|
| `codex: NOT FOUND` / `codex auth: not authenticated` | Builds can't run. | `codex login` (or install the Codex CLI). |
| `jaunt: unavailable` | Jaunt is not runnable from the project env. | `uv sync` in that project. |
| `== projects` shows STALE | Classify the bill: structural/stub = paid rebuild, prose = ~$0 refreeze, fingerprint = free re-stamp. | Run `/jaunt:build` from the owning dir. |
| orphans | Generated artifacts exist for specs that are gone. | `uv run jaunt clean --orphans`. |
| `== config drift` DRIFT | `[codex]` or `build.instructions` differs; drift restales the project but re-stamps free on the next build when specs are unchanged (only a paired structural/prose edit bills). | Make the `[codex]` and `build.instructions` blocks byte-identical BEFORE building. |
| duplicate guard hook | The plugin ships the PreToolUse guard prewired. | Delete the hand-rolled `jaunt guard` entry from `.claude/settings.json`. |

Doctor never spends money; it tells you what a build would bill. Fix drift and orphans first, then build deliberately.
