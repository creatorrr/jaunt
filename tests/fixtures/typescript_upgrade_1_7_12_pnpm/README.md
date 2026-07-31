# TypeScript project references

This example has two private npm workspace packages connected by TypeScript
project references. The app spec imports the core spec through the
`@jaunt-examples/core` package alias; generated code imports the ordinary core
facade through the same alias.

```text
packages/core  <-  packages/app  <-  plain emitted Node demo
       ^                 ^
       +------ tests ----+
```

From this directory:

```bash
npm ci
npm run --silent tooling:build
export JAUNT_TS_WORKER=../../packages/jaunt-ts/dist/worker/main.js # local checkout only
uv run --project ../.. jaunt sync --language ts
uv run --project ../.. jaunt build --language ts
uv run --project ../.. jaunt test --language ts
npm run typecheck
npm run demo
```

The same checked-in workspace is also a pnpm fixture:

```bash
pnpm install --frozen-lockfile
pnpm run typecheck
pnpm test
pnpm run demo
```

`npm run demo` builds the composite core and app projects, then runs emitted
JavaScript with Node. The emitted program has no Jaunt runtime dependency.

Installed users omit `JAUNT_TS_WORKER` and `npm run tooling:build`. Those two
steps are only needed because this example links `@usejaunt/ts` to the sibling
package checkout.
