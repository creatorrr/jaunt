---
name: working-with-jaunt
description: Use when reading or editing Python @jaunt.magic specs, private *.jaunt.ts[x] specs, jaunt.toml, generated implementations, .pyi files, API mirrors, sidecars, or Vitest batteries, or when Jaunt build/check/status appears. Explains routing, freshness, and fix-forward rules for both targets.
---

# Working with Jaunt

Jaunt is spec-driven Python and TypeScript code generation. A typed stub and its
contract prose are canonical; `jaunt build` writes validated implementations
under the configured generated directory.

## Iron rules

1. Never hand-edit `__generated__/**`, a provenance-headed generated `.pyi`,
   API mirror/sidecar, or a generated Vitest/contract battery. Edit the spec
   and use `jaunt build`, `jaunt test`, or `jaunt reconcile` to regenerate it.
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

For a version-2 `[target.ts]`, compilation ownership comes from configured
`projects`/`test_projects`, while the nearest `package.json` owns dependency
provenance. Specs are private `*.jaunt.ts[x]` inputs; consumers import the
ordinary facade. Run `jaunt sync` after adding a spec to render its API mirror
and typed unbuilt placeholder without a model call. Never edit generated
implementations, `*.api.ts`, `*.jaunt.json` sidecars, or generated test and
contract batteries. During the alpha,
`watch` follows TypeScript project/config/package inputs, and daemon jobs use
qualified `ts:` artifact keys. Review a parked proposal's exact path allowlist
before landing it; TypeScript jobs may change only the validated implementation,
API mirror, sidecar, a newly created canonical facade, and Jaunt metadata.

## Freshness taxonomy

| Reason | What the next build does |
|---|---|
| `structural` | Calls the implementation model and rebuilds the module. |
| `prose` | Calls the semantic gate, then refreezes unchanged code or rebuilds. |
| `fingerprint` / `re-stamp` | Re-stamps validated output without a model call. |
| `stub` | Re-emits the `.pyi` deterministically when implementation inputs are unchanged. |
| `unbuilt` | Keeps the typed TypeScript placeholder red in `check`; `build` calls the implementation model. |
| `invalid` | Reports compiler, conformance, sidecar, or battery diagnostics; fix the spec or toolchain before building. |

Run `status --json --progress none` through the workspace runner before a build. Describe the
likely model work, but do not invent a price. Report the actual cost printed by
the build afterward.

## TypeScript recovery boundaries (Jaunt 1.7.6+)

- `sync` and `status` validate bounded dependency batches. Strict mirrors keep
  only imports used by the public declaration surface; never add consumer-side
  lint exceptions for Jaunt placeholders or mirrors.
- A final compiler/conformance rejection is already retried inside the module's
  remaining attempt budget with the rejected source and exact diagnostics.
  Read `candidate_outcomes` in build JSON before proposing another paid run.
- Treat `JAUNT_TS_CANDIDATE_SELF_IMPORT`,
  `JAUNT_TS_GENERATED_PRIVATE_IMPORT`, and optionality/nullability TS2322
  failures as spec/prompt or generator issues. Never patch the generated
  implementation or add an import of its facade, API mirror, or generated path.
- A `WorkerOutOfMemoryError` is deterministic and is not replayed. Do not retry
  the same command unchanged or rely on `NODE_OPTIONS`; set
  `[target.ts].worker_heap_mb` to an intentional MiB value, then rerun once.
- Paid Jaunt Codex subprocesses ignore user Codex config on current CLIs, so
  this plugin's hooks and unrelated MCP tools are not nested inside generation.
  Older Codex CLIs fall back to legacy behavior; recommend an upgrade when that
  distinction matters.

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

Use the resolver's `--run` mode for every command. It prefers a compatible installed
`jaunt`, then a uv project environment, then `uvx jaunt` for a JavaScript-only
workspace.

```bash
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" specs --json
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" status --json --progress none
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" sync --language ts
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" build --json
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" check
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" clean --orphans
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" watch --test
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" jobs --json
bash <absolute-plugin-root>/scripts/resolve-workspace.sh --run "$PWD" instructions
```

Always surface build advisories. They are generator reports about ambiguous
contracts or dependency assumptions and are easy to miss in normal output.
