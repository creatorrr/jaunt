# Jaunt Codex Plugin

The Jaunt plugin gives Codex the same authoring loop Jaunt expects: edit specs,
preview stale work, build through the CLI, and review generated output without
hand-editing machine-owned files.

It is CLI-backed. There is no MCP server, app connector, or public-directory
submission in version 1.0.0.

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

Start a new Codex session after installation. Open `/hooks` to review and
trust the bundled SessionStart and PreToolUse hooks.

## Included workflows

- `$jaunt:working-with-jaunt`: workspace routing, spec authoring, freshness,
  and generated-file rules.
- `$jaunt:build`: preview likely model work, build, report actual cost, and
  run the deterministic gates.
- `$jaunt:doctor`: read-only environment, authentication, freshness, orphan,
  and duplicate-hook checks.
- `$jaunt:convert`: explicit-only conversion of handwritten Python to Jaunt.
- `$jaunt:first-build-reviewer`: explicit or delegated read-only review for
  behavior the contract leaves unstated.

The build workflow delegates a first build to one read-only explorer subagent
when that capability is available. It runs the same checklist in the main
thread otherwise.

## Hooks

The SessionStart hook reads the session `cwd` and injects a bounded freshness
summary for each discovered Jaunt workspace.

The PreToolUse hook inspects `apply_patch` paths. It denies direct edits to
files under each configured target's generated directory and to existing
provenance-headed generated `.pyi` files. TypeScript API mirrors,
implementations, and sidecars point back to their private `*.jaunt.ts[x]`
spec. Environment failures, missing configuration, malformed payloads, and
timeouts fail open. Review the hook source and trust decision in `/hooks`.

The bundled command hooks require Bash (macOS, Linux, or a Windows environment
that provides Bash). SessionStart runs `jaunt status`, which imports discovered
spec modules; trust this hook only for workspaces whose Python code you trust.
