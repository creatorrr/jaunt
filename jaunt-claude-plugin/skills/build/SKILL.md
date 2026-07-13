---
name: build
description: Use when building or rebuilding Jaunt-governed modules, after a spec edit, or when jaunt check reports stale magic artifacts. Resolves the workspace, previews model work, builds, and verifies the result.
---

# Build Jaunt specs

## 1. Resolve the workspace

```bash
bash "${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}/scripts/resolve-workspace.sh" <spec-file-or-dir>
```

Change to the printed directory. A root workspace may own several packages;
Python modules route to the nearest owning `pyproject.toml`. TypeScript modules
use configured tsconfig projects for compilation and the nearest `package.json`
for dependency ownership.

## 2. Preview the work

```bash
uv run jaunt status --json --progress none
```

Classify every stale module:

- `structural`: implementation-model rebuild.
- `prose`: semantic-gate judgment, then refreeze or rebuild.
- `fingerprint` or `re-stamp`: deterministic re-stamp.
- `stub`: deterministic `.pyi` re-emission when implementation inputs are
  unchanged.

Tell the user which modules are likely to call a model and why. Do not quote a
fixed dollar estimate. If a structural change looks accidental, stop before
the model call. Clean deleted-spec artifacts with
`uv run jaunt clean --orphans`.

For a new `*.jaunt.ts[x]` spec, run `uv run jaunt sync` before the paid build.
It writes the deterministic API mirror and an explicitly unbuilt throwing
placeholder; it does not call Codex or make `jaunt check` green.

## 3. Build

```bash
uv run jaunt build --json
# or
uv run jaunt build --target <module> --json
```

Review `newly_governed` before accepting the result.

## 4. Gate the result

1. Surface advisories verbatim.
2. Report the actual cost from the completed build.
3. Run `uv run jaunt check`.
4. Run the package's unchanged tests and target checks: Ruff/ty for Python;
   TypeScript typecheck, emit, and Vitest for TypeScript.
5. Review the generated diff as production code. Fix problems through the spec.

## 5. Review a first build

For a module's first successful build, delegate exactly one read-only explorer
subagent to the `$jaunt:first-build-reviewer` checklist. Give it the spec and
generated implementation, plus the generated `.pyi` for Python or API mirror
for TypeScript. It may read and search but must not edit or build.

If delegation is unavailable, perform the same checklist in the main thread:
compare contract to implementation for unpinned defaults, errors, ordering,
stability, mutation/copy behavior, locale/timezone/encoding assumptions, and
boundaries. Suggest one-line contract additions; never patch generated output.
