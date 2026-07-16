# Jaunt Claude Code Plugin

This plugin packages Jaunt's workspace-aware Python and TypeScript authoring
loop for Claude Code: generated-file guards, session freshness, build and
conversion skills, a read-only doctor, and a first-build reviewer.

Version 1.2.6 understands version-2 TypeScript targets as well as Python
workspace routing. One root `jaunt.toml` may cover several Python and
JavaScript packages; ownership follows the nearest `pyproject.toml` or
`package.json` for the target.

## Install

```bash
jaunt install-claude-plugin
```

From a local clone:

```bash
jaunt install-claude-plugin --local --root .
```

The direct GitHub flow is:

```bash
claude plugin marketplace add creatorrr/jaunt
claude plugin install jaunt@jaunt-plugins
```

Rerunning the installer updates the configured marketplace and uses
`claude plugin update` for an existing installation.

Start a new Claude Code session after installation so it loads the refreshed
skills and hooks.

## Included workflows

- `/jaunt:working-with-jaunt`: current spec, workspace, and freshness rules.
- `/jaunt:build`: previews likely model calls, builds, reports actual cost,
  and runs the gates.
- `/jaunt:doctor`: checks Python and TypeScript health, Node/npm, the worker,
  compiler support, authentication, orphans, active Jaunt/lock provenance, and
  Claude hook duplication without building. Nested Claude and Codex managed
  worktrees are skipped.
- `/jaunt:convert`: explicit-only Python/TypeScript-to-Jaunt conversion.
- `first-build-reviewer`: read-only review for contract-silence divergence.

For TypeScript provenance drift, the working skill previews
`jaunt migrate --language ts --json` before any paid build. A plan containing
only `free-recompose` actions with an empty `requires_rebuild` list can be
applied and checked without model calls.

For TypeScript builds, the workflow reads `candidate_outcomes` before suggesting
another run. Jaunt already spends the remaining attempt budget on final
conformance repair; a failed module should not be rerun blindly. Worker heap
failures point to `[target.ts].worker_heap_mb` and are never replayed
automatically.

For TypeScript test generation, inspect `vitest.batteries` before another paid
run (`targets.ts.vitest.batteries` in a mixed workspace). It records per-battery
retries and final rejection reasons. A failed combined overlay can still commit
the compatible subset listed under `vitest.partial_landing` or
`targets.ts.vitest.partial_landing`.

The SessionStart hook reports the nearest active Jaunt workspace, including
TypeScript unbuilt, invalid, and diagnostic state. If no parent `jaunt.toml`
exists, it falls back to bounded descendant discovery. The PreToolUse hook
keeps Claude's approval-style guard for each target's generated directory and
existing provenance-headed `.pyi` files. TypeScript API mirrors,
implementations, and sidecars point back to their private `*.jaunt.ts[x]`
spec. Both hooks fail open on malformed input, missing configuration,
unavailable tools, or timeouts.

There is no MCP server. Jaunt's JSON CLI is the machine interface.

The command hooks use Bash when it is available. Both launchers always return
success to the host, so a missing Bash silently disables the hook instead of
failing the session. SessionStart stays inside the active Git worktree (or
nearest parent `jaunt.toml`) and ignores nested Claude/Codex managed worktrees
and repositories. It runs `jaunt status`, which imports discovered spec modules;
enable it only for workspaces whose Python code you trust.
CLI calls prefer a compatible installed `jaunt`, use the existing uv environment for a uv
project, and otherwise use `uvx jaunt` in JavaScript-only projects.
