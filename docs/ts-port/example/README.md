# Jaunt-for-TypeScript — runnable design preview

A hand-built mock of what a Jaunt TypeScript target would look like, wired
end-to-end so every design choice in [`../DESIGN.md`](../DESIGN.md) can be
run, typechecked, and tested today. **There is no TS builder yet**: the
files under `__generated__/` are written by hand to look exactly like what
`jaunt build` / `jaunt test` / `jaunt reconcile` would emit.

```bash
npm install
npm run typecheck   # tsc --noEmit — includes the conformance boundary
npm test            # vitest — plain imports, no plugin, no resolver
npm run demo        # plain `node src/app.ts` — no hooks, no jaunt runtime
```

Requires Node >= 22.18 (native type-stripping to run `.ts` directly).

## The module layout (the load-bearing choice)

After an external design review, substitution moved from resolve-time hooks
to an **explicit generated facade** — one module graph, ordinary resolution,
no jaunt runtime dependency for consumers:

```
src/tokens/
  spec.jaunt.ts        authored contracts + stubs; never imported at runtime
  context.ts           handwritten executable context (ordinary module)
  index.ts             ordinary public facade — what consumers import
  __generated__/
    impl.ts            generated implementation, authored-type-annotated
```

Three properties make this work:

1. **The spec is types-only at runtime.** `impl.ts` reaches it exclusively
   through erased positions (`import type`, `typeof import(...)`), so the
   stubs never load and there is no raw-spec evaluation.
2. **Conformance is the export annotation itself.**
   `export const createToken: typeof import("../spec.jaunt.ts").createToken = createTokenImpl;`
   both *enforces* assignability (drift = compile error — verified: changing
   the impl's `userId` to `number` fails tsc on exactly that line) and
   *pins* the consumer-facing type to the authored contract.
3. **Executable handwritten code lives in `context.ts`, not the spec.**
   That rule eliminates the lexical-binding trap by construction: nothing
   that runs can accidentally close over a stub.

A missing build is an honest compile failure (the facade's import of
`__generated__/impl.ts` doesn't resolve), not a runtime mystery.

## What each file demonstrates

| Design choice | File |
|---|---|
| `magicModule()` pragma + `jaunt.magic()` stub bodies + deps as real identifiers | `src/tokens/spec.jaunt.ts` |
| TSDoc as the contract; interfaces/types as spec-resident type context | `src/tokens/spec.jaunt.ts` |
| `jaunt design` outcome: model-proposed declaration accepted into the spec | `TokenStore` in `src/tokens/spec.jaunt.ts` |
| Handwritten executable context as an ordinary module | `src/tokens/context.ts` |
| Ordinary public facade | `src/tokens/index.ts` |
| Generated impl with authored-type-annotated exports (conformance boundary) | `src/tokens/__generated__/impl.ts` |
| Contract mode via `@jauntContract`; `@example`/`@throws`/`@prop` sections | `src/tokens/b64url.ts` |
| Runtime shim the `jaunt` npm package would ship (markers + placeholder error) | `src/jaunt/index.ts` |
| Optional dev-only resolver (NOT in the correctness path) | `src/jaunt/register.mjs` |
| Authored test specs (`jaunt.testSpec` bodies) | `tests/token-specs.ts` |
| Generated tests, tiered by filename (`.example.` / `.derived.`) | `tests/__generated__/*.test.ts` |
| Fixtures as `test.extend` (the conftest.py analog, compile-checked) | `tests/fixtures.ts` |
| Derived contract battery with seeded fast-check property | `tests/contract/b64url.contract.test.ts` |
| TOML config with `[target.ts]` + `spec_suffix` for mixed py/ts repos | `jaunt.toml` |

## Deliberately not shown

- The builder/scanner themselves (this preview fakes their *output*).
- The `jaunt design` review flow (only its end state — TokenStore's
  accepted declaration — is shown).
- The held-out vitest reporter (tier redaction is described in the generated
  test headers; the reporter is a ~100-line custom reporter).
- Mutation strength scoring (worker_threads + `worker.terminate()` replaces
  `SIGALRM`; mechanical AST mutants as in `contract/strength.py`).
- The semantic gate and prompt templates (unchanged in structure from the
  Python port; content rewritten for TS).
