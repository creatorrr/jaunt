---
name: working-with-jaunt
description: Use when reading or editing a Jaunt spec module, jaunt.toml, generated code, or a generated .pyi stub, or when jaunt build/check/status appears in the task. Explains workspace routing, freshness, and fix-forward rules.
---

# Working with Jaunt

Jaunt is spec-driven Python code generation. A typed stub and its docstring are
the contract; `jaunt build` writes the implementation under `__generated__/`.

## Iron rules

1. Never hand-edit `__generated__/**` or a provenance-headed generated
   `.pyi`. Edit the spec, rebuild, and review the regenerated files.
2. Never change an existing test merely to accommodate generated code. Tighten
   the contract when generated behavior misses an established expectation.
3. Resolve the workspace before running Jaunt:
   locate this installed `SKILL.md`, walk up two directories to the plugin
   root, and run its `scripts/resolve-workspace.sh` by absolute path. Do not
   assume `PLUGIN_ROOT` is set in ordinary skill-driven shell calls.
4. Treat a fresh spec as canonical. Read generated code for build review or
   debugging, not as the authoring surface.

## Workspace routing (Jaunt 1.6.2+)

One root `jaunt.toml` can cover several packages and mixed flat/`src`
layouts. `source_roots` and `test_roots` accept literal paths and globs.
Jaunt routes each module through its longest containing root, then uses the
nearest owning `pyproject.toml` for generated code, stubs, dependency
validation, tests, and contract batteries. A workspace-root dependency does
not excuse an undeclared child-package dependency.

Use `jaunt migrate --merge-projects` to preview consolidation of older child
configs. Add `--apply` only after the no-model plan reports no route, digest,
fingerprint, or artifact change.

## Freshness taxonomy

| Reason | What the next build does |
|---|---|
| `structural` | Calls the implementation model and rebuilds the module. |
| `prose` | Calls the semantic gate, then refreezes unchanged code or rebuilds. |
| `fingerprint` / `re-stamp` | Re-stamps validated output without a model call. |
| `stub` | Re-emits the `.pyi` deterministically when implementation inputs are unchanged. |

Run `uv run jaunt status --json --progress none` before a build. Describe the
likely model work, but do not invent a price. Report the actual cost printed by
the build afterward.

## Authoring specs

- Prefer `jaunt.magic_module(__name__)` plus top-level strict stubs. Valid
  forms include `...`, a bare docstring, `pass`, and
  `raise NotImplementedError`. Preserve the body form when a digest-neutral
  decorator-to-module migration matters.
- A real body or non-Jaunt decorator stays handwritten context.
- Use `@jaunt.magic` for per-symbol `deps=` or `prompt=` overrides.
- Inside a whole-class spec, `@jaunt.preserve` keeps a handwritten method,
  `@jaunt.sig` seals a generated signature, and an unmarked stub is a
  guidepost the model may adapt.
- Keep import-time calls, instantiation, and subclassing of module-governed
  specs inside functions; module-level consumers see the pre-rebind stub.
- State every behavior that callers and tests rely on, including failure types,
  ordering, mutation, boundary behavior, and time-dependent assumptions.

## Useful commands

```bash
uv run jaunt specs --json
uv run jaunt status --json --progress none
uv run jaunt build --json
uv run jaunt check
uv run jaunt clean --orphans
uv run jaunt instructions
```

Always surface build advisories. They are generator reports about ambiguous
contracts or dependency assumptions and are easy to miss in normal output.
