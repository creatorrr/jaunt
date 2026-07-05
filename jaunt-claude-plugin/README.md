# Jaunt Claude Code Plugin

Claude Code plugin for [Jaunt](https://github.com/creatorrr/jaunt): generated-code
guardrails, session freshness, project health checks, conversion workflow, and
cost-aware build discipline for 1.5-era Jaunt projects.

Organizing principle: **make the paid paths deliberate and the free paths
frictionless** — builds bill real money; status/check/re-stamp are free.

Docs: https://jaunt.ing/docs/guides/claude-code-plugin

## What's included

| Component | What it does | Scar it heals |
|---|---|---|
| `hooks/hooks.json` PreToolUse | Wires `scripts/guard.sh` on Edit/MultiEdit/Write/NotebookEdit — an owning-project wrapper around `jaunt guard` for `__generated__/**` + generated `.pyi` | Every adopter hand-rolls this guard |
| `hooks/hooks.json` SessionStart | `scripts/session-status.sh`: freshness map per jaunt project (stale + why, orphans) injected at session start | Drift discovered only when someone thinks to look |
| `skills/working-with-jaunt/` | Auto-invoked knowledge: digest taxonomy + cost table, stub forms, self-contained contracts, multi-project byte-identical-config rule | Each adopter re-derives all of this |
| `skills/build/` | `/jaunt:build`: resolve owning project → classify staleness before spending → build → advisories/check/tests/first-build line-review | Wrong-cwd misroutes; advisories scrolling away; unreviewed first builds |
| `skills/doctor/` | `/jaunt:doctor`: env + config-drift + orphans + duplicate-hook health check; free and deterministic (no model calls, no builds) | Drift/orphans/auth problems discovered only at build time |
| `skills/convert/` | `/jaunt:convert`: conversion protocol — churn triage, characterization tests first, contract distillation, stub, build, gate, first-build review | Ad-hoc conversions billing before a safety net exists |
| `agents/first-build-reviewer.md` | Adversarial first-build review for contract-silence divergence | Behavior the docstring doesn't pin, caught by no deterministic gate |
| `scripts/guard.sh` | Resolves the OWNING jaunt project from the payload path, then runs `jaunt guard` from there | Wrong-project guard config (`generated_dir`) in multi-project repos |
| `scripts/resolve-project.sh` | File → owning jaunt project dir | Multi-project routing (pre-1.6) |

Fail-open on purpose: both hooks swallow env errors (`|| true`, `exit 0`)
so a broken uv env never blocks editing or session start.

Unlike the 0.4.x plugin, there is no MCP server: the CLI's `--json` flags
already give agents structured access, and a server lifecycle adds surface
without capability.

## Installation

From the GitHub marketplace:

```bash
claude plugin marketplace add creatorrr/jaunt
claude plugin install jaunt@jaunt-plugins
```

From a clone, at the repo root:

```bash
claude plugin marketplace add .
claude plugin install jaunt@jaunt-plugins
```

For one session: `claude --plugin-dir ./jaunt-claude-plugin`

Repos with a hand-rolled `jaunt guard` hook in `.claude/settings.json` can
delete it once the plugin loads — it becomes a duplicate.

## Roadmap

Doctor, convert, and the first-build reviewer shipped in 1.0.0; they were the
old roadmap items 1–3.

1. Monitor on the propose-only daemon — "proposal ready" notifications →
   event-driven `jobs land` instead of polling.
2. Cost ledger in `${CLAUDE_PLUGIN_DATA}` feeding real spend estimates into
   the build skill.

Meta-wish for jaunt itself: a machine-readable build report
(`.jaunt/last-build.json`: advisories, cost, stale reasons, routing) would
shrink most of this plugin to thin glue.

History: revived the plugin dropped in the pre-1.0 cleanup (#55), redesigned
around 1.5-era semantics; grew out of the mem-mcp adoption campaign (FEEDBACK
findings 27–29).
