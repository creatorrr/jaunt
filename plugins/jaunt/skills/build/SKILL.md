---
name: build
description: Use when building or rebuilding Jaunt-governed modules, after a spec edit, or when jaunt check reports stale magic artifacts. Resolves the workspace, previews model work, builds, and verifies the result.
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
Jaunt routes each module to the nearest owning `pyproject.toml`.

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
4. Run the package's unchanged tests, Ruff, and ty.
5. Review the generated diff as production code. Fix problems through the spec.

## 5. Review a first build

For a module's first successful build, delegate exactly one read-only explorer
subagent to the `$jaunt:first-build-reviewer` checklist. Give it the spec,
generated implementation, and generated `.pyi` path. It may read and search
but must not edit or build.

If delegation is unavailable, perform the same checklist in the main thread:
compare contract to implementation for unpinned defaults, errors, ordering,
stability, mutation/copy behavior, locale/timezone/encoding assumptions, and
boundaries. Suggest one-line contract additions; never patch generated output.
