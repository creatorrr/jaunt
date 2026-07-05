---
name: build
description: Use when building or rebuilding jaunt-governed modules (jaunt build, regenerating after a spec edit, or a stale jaunt check). Plan-first, cost-aware build workflow — resolves the owning project, classifies staleness before spending, and gates the result.
---

# /jaunt:build — plan-first build

Builds spend real money (model calls, $1–$20+ per structurally-stale module).
This workflow makes the paid path deliberate and the free path frictionless.

## 1. Resolve the owning project

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/resolve-project.sh" <spec-file-or-dir>
```

cd to the printed directory. All jaunt commands below run from there —
running from the wrong project routes output and the check gate against the
wrong source roots.

## 2. Classify before spending

```bash
uv run jaunt status --json --progress none
```

For each stale module, read `stale_changes`:

- **fingerprint-only** (tool version / config change) → free model-less
  re-stamp; just build.
- **structural or prose** → paid model call. Before building, tell the user
  which modules will bill and why. If the staleness looks accidental (e.g. a
  drive-by signature or `prompt=` touch), stop and confirm — the fix may be
  reverting the edit, not paying for a rebuild.

Also check `orphans` — a deleted spec needs `uv run jaunt clean --orphans`,
not a build.

## 3. Build

```bash
uv run jaunt build --json            # everything stale in this project
uv run jaunt build --target <module> # scoped
```

The build plan flags **newly-governed symbols** before spend — read that list;
an unexpected entry usually means an accidental docstring-only stub.

## 4. Gate the result (non-negotiable)

1. **Surface advisories verbatim** to the user — they are the generator
   flagging spec ambiguities and suspected dep bugs, and they scroll away.
2. Report the cost line.
3. `uv run jaunt check` in this project (must exit 0).
4. Run the package's existing tests unchanged — plus ruff/ty. The
   pre-existing suite is the behavioral safety net; never edit tests to
   accommodate generation.
5. Review the `__generated__/` diff like code you're accountable for. On a
   module's FIRST build, dispatch the plugin's `first-build-reviewer` agent
   with the spec path + generated file path; apply its suggested docstring
   additions, but say the cost first: prose-class additions refreeze ~$0,
   structural ones re-bill on the next build. The failure class no gate can
   catch is behavior the spec doesn't pin (contract-silence divergence).

## Failure triage

| Symptom | Likely cause | Fix |
|---|---|---|
| Generated import rejected as undeclared | Dep missing from the OWNING package's pyproject (≥1.5.1 resolves from spec-owning + config-root pyprojects) | Declare it there, or import it in the spec module |
| Exit 2, multi-root error | Governed specs span source roots | One jaunt project per package (per-module roots come in 1.6) |
| ty errors in generated code | Contract not self-contained (missing types/invariants) | Tighten the docstring; inline cross-module context into `magic_module(prompt=...)` |
| Existing tests fail | Docstring didn't pin the behavior the tests expect | Add the rule/example to the docstring, rebuild. Never patch the body |
| Everything restaled after a config touch | `[codex]`/`[build].instructions` drift between project configs | Make them byte-identical again before building |
