# Jaunt Claude Code Plugin

Claude Code plugin for [Jaunt](https://github.com/creatorrr/jaunt). Revives the
plugin dropped in the pre-1.0 cleanup (#55), redesigned around 1.5-era
semantics: module-style specs, the digest/cost taxonomy, and multi-project
repos. Grew out of a real adoption campaign (mem-mcp, findings 27–29 in
FEEDBACK.md); every component maps to friction actually hit there.

Organizing principle: **make the paid paths deliberate and the free paths
frictionless** — builds bill real money; status/check/re-stamp are free.

## What's included

| Component | What it does | Scar it heals |
|---|---|---|
| `hooks/hooks.json` PreToolUse | Wires `jaunt guard` (ships with jaunt) on Edit/Write — "ask" gate on `__generated__/**` + generated `.pyi`, pointing at the owning spec | Every adopter hand-rolls this guard |
| `hooks/hooks.json` SessionStart | `scripts/session-status.sh`: freshness map per jaunt project (stale + why, orphans) injected at session start | Drift discovered only when someone thinks to look |
| `skills/working-with-jaunt/` | Auto-invoked knowledge: digest taxonomy + cost table, stub forms, self-contained contracts, multi-project byte-identical-config rule | Each adopter re-derives all of this |
| `skills/build/` | `/jaunt:build`: resolve owning project → classify staleness before spending → build → advisories/check/tests/first-build line-review | Wrong-cwd misroutes; advisories scrolling away; unreviewed first builds |
| `scripts/resolve-project.sh` | File → owning jaunt project dir | Multi-project routing (pre-1.6) |

Fail-open on purpose: both hooks swallow env errors (`|| true`, `exit 0`)
so a broken uv env never blocks editing or session start.

Unlike the 0.4.x plugin, there is no MCP server: the CLI's `--json` flags
already give agents structured access, and a server lifecycle adds surface
without capability.

## Installation

From a clone:

```bash
claude plugin marketplace add ./jaunt-claude-plugin
claude plugin install jaunt@jaunt-plugins
```

Or for one session: `claude --plugin-dir ./jaunt-claude-plugin`

Repos with a hand-rolled `jaunt guard` hook in `.claude/settings.json` can
delete it once the plugin loads — it becomes a duplicate.

## Roadmap (deliberately not in this MVP)

1. `/jaunt:convert` — full conversion protocol (churn triage → characterization
   tests first → distill → build → gate → line-review).
2. `/jaunt:doctor` — cross-project `[codex]`/`[build].instructions`
   byte-equality, orphans, pin-vs-installed drift.
3. `first-build-reviewer` agent — adversarial review for contract-silence
   divergence.
4. Monitor on the propose-only daemon — "proposal ready" notifications →
   event-driven `jobs land` instead of polling.
5. Cost ledger in `${CLAUDE_PLUGIN_DATA}` feeding real spend estimates into
   the build skill.

Meta-wish for jaunt itself: a machine-readable build report
(`.jaunt/last-build.json`: advisories, cost, stale reasons, routing) would
shrink most of this plugin to thin glue.
