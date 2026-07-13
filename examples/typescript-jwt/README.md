# Jaunt-for-TypeScript end-to-end fixture

This JWT project exercises the TypeScript target through its real worker and Python
orchestrator. Jaunt renders the API mirror, asks Codex for the implementation and test
batteries, checks candidate overlays before writing them, and derives the committed
contract batteries. The checked-in generated files carry current Jaunt provenance.

The fixture uses the local `@usejaunt/ts/spec` package through a `file:` development
dependency. Nothing under the production facade imports that marker package or the
private spec.

## Run it

```bash
cd ../../packages/jaunt-ts
npm ci
npm run build          # the fixture uses this checkout through a file: dependency
cd ../../examples/typescript-jwt
npm ci
npm run typecheck       # production/tests plus a non-emitting private-input program
npm test                # generated Vitest example/derived/contract batteries
npm run emit            # ordinary JS plus declaration emit into dist/
npm run demo            # rebuild, then run node dist/app.js
npm run smoke:consumer  # pack, install, typecheck, and execute a clean consumer
```

No command uses a loader, resolver hook, ts-node, tsx, or Node's direct TypeScript
execution. The demo and clean consumer run emitted JavaScript.

To regenerate the Jaunt-owned files from this checkout, build the worker first and
point the Python orchestrator at it:

```bash
export JAUNT_TS_WORKER="$PWD/../../packages/jaunt-ts/dist/worker/main.js"

uv run --project ../../.. jaunt sync --root . --language ts
uv run --project ../../.. jaunt build --root . --language ts
uv run --project ../../.. jaunt test --root . --language ts
uv run --project ../../.. jaunt reconcile --root . --language ts
uv run --project ../../.. jaunt check --root . --language ts
```

`build`, `test`, and `reconcile` call the configured Codex model. `sync` and `check`
are deterministic and make no model call. An installed `@usejaunt/ts` worker does not
need the `JAUNT_TS_WORKER` override.

## Module layout

```text
src/tokens/
  index.jaunt.ts              private authored contracts and stubs; never emitted
  index.context.ts            handwritten runtime leaf
  index.ts                    committed public facade
  __generated__/
    index.api.ts              deterministic declaration mirror; no runtime behavior
    index.ts                  generated implementation
    index.jaunt.json          build inputs, digests, and provenance
```

The public facade is deliberately boring:

```ts
export type { Claims, JwtErrorCode } from "./__generated__/index.api.js";
export * from "./index.context.js";
export * from "./__generated__/index.js";
```

Every source import uses its runtime `.js` specifier. TypeScript resolves these
to source `.ts` files while checking, and the same specifiers work unchanged in
`dist/`.

## What the proof establishes

- `index.jaunt.ts` is included only in the non-emitting analysis Program.
  The configured production and test projects exclude `*.jaunt.ts[x]` and
  `*.jaunt-test.ts[x]`; `tsconfig.analysis.json` mirrors the worker's private,
  non-emitting analysis Program for this fixture.
- `index.api.ts` carries the authored declarations and TSDoc into the production
  graph. Context, implementation, facade declarations, and consumers never
  reference the raw spec.
- `index.context.ts` is a one-way runtime leaf. It may use mirror types but does
  not value-import the facade, implementation, or spec.
- The generated implementation exposes deterministic, mirror-typed exports.
  Type-only per-member adapters reconstruct class methods as strict function
  types, so narrowed constructor or method inputs cannot hide behind
  TypeScript's bivariant method checking. `tests/class-conformance.types.ts`
  contains a negative compile sentinel for that regression.
- The packed library contains emitted JS and declarations only. The consumer
  smoke rejects private spec paths and Jaunt runtime references, then installs
  the tarball in a new temporary project and compiles and runs it.

## Test and contract inputs

- `tests/tokens.jaunt-test.ts` is private authored test intent. Its suffix keeps
  it out of Vitest collection and every emitting program.
- `tests/__generated__/*.example.test.ts` retains normal failure detail.
- `tests/__generated__/*.derived.test.ts` is the held-out tier. Jaunt's protected
  runner redacts its repair feedback.
- `tests/fixtures.ts` is the typed `test.extend` fixture surface.
- `src/tokens/b64url.ts` is committed contract-mode code. `jaunt reconcile` derives
  one battery per adopted export under `tests/contract/src/tokens/`, then requires
  every applicable mutation to be killed before it writes either battery.
