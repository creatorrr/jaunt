# Jaunt Codex Plugin

The Jaunt plugin gives Codex the same Python and TypeScript authoring loop Jaunt
expects: edit specs, preview stale or unbuilt work, build through the CLI, and
review generated output without hand-editing machine-owned files.

It is CLI-backed. There is no MCP server, app connector, or public-directory
submission in version 1.1.6.

## Install

From GitHub:

```bash
jaunt install-codex-plugin
```

From a local clone:

```bash
jaunt install-codex-plugin --local --root .
```

The direct commands are:

```bash
codex plugin marketplace add creatorrr/jaunt
codex plugin add jaunt@jaunt-codex-plugins
```

On a rerun, the installer upgrades the Git marketplace snapshot and re-adds
the plugin, which refreshes the cache on current Codex releases. If an older
CLI reports the plugin as already installed, the installer removes and re-adds
it. Local mode skips the Git upgrade but performs the same cache refresh.

Start a new Codex session after installation. Open `/hooks` to review and
trust the bundled SessionStart and PreToolUse hooks.

## Included workflows

- `$jaunt:working-with-jaunt`: workspace routing, spec authoring, freshness,
  and generated-file rules.
- `$jaunt:build`: preview likely model work, build, report actual cost, and
  run the deterministic gates.
- `$jaunt:doctor`: read-only environment, authentication, freshness, orphan,
  Codex-hook, and active Jaunt/lock provenance checks. Nested Claude and Codex
  managed worktrees are skipped.
- `$jaunt:convert`: explicit-only conversion of handwritten Python or
  TypeScript to Jaunt.
- `$jaunt:first-build-reviewer`: explicit or delegated read-only review for
  behavior the contract leaves unstated.

The build workflow delegates a first build to one read-only explorer subagent
when that capability is available. It runs the same checklist in the main
thread otherwise.

For TypeScript provenance drift, the working skill previews
`jaunt migrate --language ts --json` before any paid build. A plan containing
only `free-recompose` actions with an empty `requires_rebuild` list can be
applied and checked without model calls.

Battery drift that includes `target_api_digest` plus `battery_fingerprint` has a
second free route, including co-drift in the prompt, runner, or Vitest fingerprint:
run `jaunt test --language ts --no-build` and then `jaunt check --language ts`.
A green committed body is verified and reheadered; `--no-run` deliberately disables
that proof.

For TypeScript builds, the workflow reads `candidate_outcomes` before suggesting
another run. Jaunt already spends the remaining attempt budget on final
conformance repair; a failed module should not be rerun blindly. Worker heap
failures point to `[target.ts].worker_heap_mb` and are never replayed
automatically.

For TypeScript test generation, inspect `vitest.batteries` before another paid
run (`targets.ts.vitest.batteries` in a mixed workspace). It records per-battery
retries and final rejection reasons. A failed combined overlay can still commit
the compatible subset listed under `vitest.partial_landing` or
`targets.ts.vitest.partial_landing`. Terminal exhaustion preserves the exact
candidate and metadata under `.jaunt/typescript/rejected-tests/`.
Runner outages and a bad committed baseline retain validated live or cached
candidates for recovery instead of spending another attempt or evicting them.

## Hooks

The SessionStart hook reads the session `cwd` and reports the nearest active
Jaunt workspace, including TypeScript unbuilt, invalid, and diagnostic counts.
If no parent `jaunt.toml` exists, it falls back to bounded descendant discovery.
Doctor also checks Node, npm, the project-local
`@usejaunt/ts` worker, and the supported compiler range without a model call.

The PreToolUse hook inspects `apply_patch` paths. It denies direct edits to
files under each configured target's generated directory and to existing
provenance-headed generated `.pyi` files. TypeScript API mirrors,
implementations, and sidecars point back to their private `*.jaunt.ts[x]`
spec. Environment failures, missing configuration, malformed payloads, and
timeouts fail open. Review the hook source and trust decision in `/hooks`.

The bundled command hooks use Bash when it is available. Both launchers always
return success to the host, so a missing Bash silently disables the hook instead
of failing the session. SessionStart stays inside the active Git worktree (or
nearest parent `jaunt.toml`) and ignores nested Claude/Codex managed worktrees
and repositories. It runs `jaunt status`, which imports discovered spec modules;
trust this hook only for workspaces whose Python code you trust.
CLI calls prefer a compatible installed `jaunt`, use the existing uv environment for a uv
project, and otherwise use `uvx jaunt`, so JavaScript-only projects do not need
a `pyproject.toml`.
