---
name: build
description: Use when building or rebuilding Python or TypeScript Jaunt specs, after editing @jaunt.magic or *.jaunt.ts[x], or when check reports stale or unbuilt artifacts. Resolves the workspace, previews model work, builds, and verifies native tests and type checks.
---

# Build Jaunt specs

## 1. Resolve the workspace

```bash
bash <absolute-plugin-root>/scripts/resolve-workspace.sh <spec-file-or-dir>
```

Resolve `<absolute-plugin-root>` by locating this installed `SKILL.md` and
walking up two directories. Do not assume `PLUGIN_ROOT` is set in an ordinary
skill-driven shell call.

Change to the printed directory. A root workspace may own several packages;
Python modules route to the nearest owning `pyproject.toml`. TypeScript modules
use configured tsconfig projects for compilation and the nearest `package.json`
for dependency ownership.

Run every Jaunt command below through the same script with `--run`. It prefers
a compatible installed `jaunt`, then `uv run --no-sync jaunt` when the workspace is a uv
project, then `uvx jaunt` for a JavaScript-only checkout.

## 2. Preview the work

```bash
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" status --json --progress none
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
`clean --orphans` through the selected runner.

For a new `*.jaunt.ts[x]` spec, run `sync --language ts` through the selected runner before the paid build.
It writes the deterministic API mirror and an explicitly unbuilt throwing
placeholder; it does not call Codex or make `jaunt check` green.

## 3. Build

```bash
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" build --json
# or
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" build --target <qualified-module> --json
```

Review `newly_governed` before accepting the result.

For a TypeScript build, inspect `candidate_outcomes` for every attempted module.
`attempts`, `retry_count`, `retry_reasons`, and `phase` distinguish a repaired
candidate from an exhausted budget. Jaunt performs final conformance retries
inside this command and charges each attempt separately. Do not rerun an
unchanged failed target just to consume the same budget again; fix the spec or
its explicit module prompt when the reported reason is contractual.

If the command reports a deterministic worker heap OOM, do not replay it
unchanged. Set `[target.ts].worker_heap_mb` to a deliberate MiB limit and rerun
once. `NODE_OPTIONS` is intentionally not forwarded to the worker.

## 4. Gate the result

1. Surface advisories verbatim.
2. Report the actual cost and per-module attempt outcome from the completed build.
3. Run `check` through the selected runner.
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
