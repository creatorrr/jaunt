# Jaunt-for-TypeScript — runnable design preview

A hand-built mock of what a Jaunt TypeScript target would look like, wired
end-to-end so every design choice in [`../DESIGN.md`](../DESIGN.md) can be
run, typechecked, and tested today. **There is no TS builder yet**: the
files under `__generated__/` and the `.jaunt/ts-manifest.json` are written
by hand to look exactly like what `jaunt build` / `jaunt test` /
`jaunt reconcile` would emit.

```bash
npm install
npm run typecheck      # tsc --noEmit — includes the satisfies conformance file
npm test               # vitest, resolved through the jaunt redirect plugin
npm run demo           # node + the registerHooks resolver: full flow works
npm run demo:prebuild  # no resolver: first spec call throws JauntNotBuiltError
```

Requires Node >= 22.18 (`module.registerHooks` + native type-stripping).

## What each file demonstrates

| Design choice | File |
|---|---|
| `magicModule()` pragma + `jaunt.magic()` stub bodies + deps as real identifiers | `src/tokens/specs.ts` |
| TSDoc as the contract; interfaces/real bodies as handwritten context | `src/tokens/specs.ts` |
| Designed API (docstring-only class) | `TokenStore` in `src/tokens/specs.ts` |
| Contract mode via `@jauntContract` tag; `@example`/`@throws`/`@prop` sections | `src/tokens/b64url.ts` |
| Mock generated output (header, context re-exports, internal-helper freedom) | `src/tokens/__generated__/specs.ts` |
| Checker-enforced conformance (`satisfies`) replacing `@jaunt.sig` | `src/tokens/__generated__/specs.check.ts` |
| Barrel: declared APIs re-export from spec, designed APIs from `__generated__` | `src/tokens/index.ts` |
| Runtime shim the `jaunt` npm package would ship | `src/jaunt/index.ts` |
| Node resolution adapter (`registerHooks`, importer-aware exception) | `src/jaunt/register.mjs` |
| Vite/Vitest twin of the same redirect rule | `vitest.config.ts` |
| Build manifest the resolvers consume | `.jaunt/ts-manifest.json` |
| Authored test specs (`jaunt.testSpec` bodies) | `tests/token-specs.ts` |
| Generated tests, tiered by filename (`.example.` / `.derived.`) | `tests/__generated__/*.test.ts` |
| Fixtures as `test.extend` (the conftest.py analog, compile-checked) | `tests/fixtures.ts` |
| Derived contract battery with seeded fast-check property | `tests/contract/b64url.contract.test.ts` |
| TOML config with `[target.ts]` for mixed-language repos | `jaunt.toml` |

## The two demo modes, and why they matter

`npm run demo` loads `src/jaunt/register.mjs`, which redirects imports of
governed spec modules to their `__generated__` siblings — the ESM-native
replacement for Python jaunt's live module rebinding. The demo exercises the
full flow: create/verify/rotate through the spec path, plus the designed
`TokenStore` through the barrel.

`npm run demo:prebuild` runs without the resolver: the first call to a spec
stub throws `JauntNotBuiltError` with a message pointing at both fixes
(build, or install the resolver). Note the designed API still works in this
mode — the barrel binds it straight to `__generated__`. This is the
graceful-degradation story: nothing silently half-works.

## Deliberately not shown

- The builder/scanner themselves (this preview fakes their *output*).
- The held-out vitest reporter (tier redaction is described in the generated
  test headers; the reporter is a ~100-line custom reporter).
- Mutation strength scoring (worker_threads + `worker.terminate()` replaces
  `SIGALRM`; mechanical AST mutants as in `contract/strength.py`).
- The semantic gate and prompt templates (unchanged in structure from the
  Python port; content rewritten for TS).
