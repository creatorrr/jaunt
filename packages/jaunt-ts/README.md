# @usejaunt/ts

The TypeScript target for
[Jaunt](https://github.com/creatorrr/jaunt), a spec-driven code-generation
framework. This alpha ships the marker API, static analyzer worker, contract IR,
deterministic API mirrors and unbuilt placeholders, overlay conformance checks,
orphan discovery, and the isolated Vitest runner used by Jaunt's Python
orchestrator.

Spec modules are private analysis inputs. Importing one at runtime throws
`JauntNotBuiltError`; applications import the ordinary facade that Jaunt creates
next to the spec.

```ts
import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/** Convert a title to a stable URL slug. */
export function slugify(title: string): string {
  return jaunt.magic();
}
```

The analyzer loads the compiler installed in the workspace instead of bundling a
second TypeScript. The current supported range is TypeScript `>=5.8 <7`. TypeScript
7's CLI can build this package, but its new native implementation does not yet
offer the stable programmatic API Jaunt needs; the worker rejects it with a clear
diagnostic.

The worker and protected test runner require Node `>=20 <25`. This is the tool-host
range, not a restriction on the runtime used to deploy generated JavaScript. CI
tests both boundary majors, Node 20 and Node 24.

Pin a supported compiler when installing; an unversioned `typescript` install may
select TypeScript 7:

```bash
npm install -D @usejaunt/ts@next 'typescript@^5.9' vitest fast-check @types/node
```

Declare `vitest` directly in every package that owns a test project, and declare
`fast-check` there when the package uses property intent. Jaunt does not treat a
hoisted-but-undeclared package as permission to use it.

The supported property form is
`@prop given name: type-or-strategy :: left equals right` (or
`does not equal`). Strategies may compose fast-check calls such as
`fc.integer({ min: 0, max: 100 })`, `fc.tuple(...)`, and `fc.record(...)` from
data literals. Jaunt parses and renders the typed case before the model runs;
arbitrary executable prose is rejected.

Exports:

- `@usejaunt/ts/spec` — `magicModule`, `magic`, `testSpec`, and the runtime guard,
  with separate ESM and CommonJS declaration conditions.
- `@usejaunt/ts/worker` / `jaunt-ts-worker` — the versioned JSONL analyzer protocol.
- `@usejaunt/ts/test-runner` — one-shot typecheck and Vitest execution.
- `@usejaunt/ts/schema/protocol-v1` and `contract-ir-v1` — the pinned draft schemas.

The protocol is deliberately strict: each request carries
`"protocol":"jaunt-ts/1-draft.2"`, a string ID, a method, and an object of params.
Initialize first, then analyze the workspace or contracts, validate a candidate
overlay or deterministic sync, project committed contract declarations through the
compiler AST, inspect orphans, and shut the worker down. `projectContract` preserves
TSDoc and exact exported signatures while removing executable bodies and initializers;
malformed or still-executable projections fail closed. Every analysis response includes
an epoch, snapshot, and per-input hashes so stale writes can be rejected.

The compatibility matrix exercises NodeNext ESM, NodeNext CommonJS,
Bundler/Vite-style resolution, and `.tsx` under both TypeScript 5.8 and 6.x. The
generated program uses the owning project's module and JSX settings; it does not
gain an `@usejaunt/ts` runtime dependency.

## Performance gate

`npm run benchmark:watch` builds a pinned 1,000-file graph and drives one worker
through 100 edit, invalidate, and analysis cycles. It writes JSON when passed an
output path:

```bash
npm run benchmark:watch -- --output /tmp/jaunt-ts-watch.json
```

The JSON includes p50/p95 timings, peak RSS, post-GC RSS slope, open file
descriptors, active Node resources, listeners, child processes, worker restarts,
and surviving processes. `npm run benchmark:watch` strictly enforces the numeric
resource budgets and a 20% ceiling over the rounded first-alpha timing baseline
for a controlled Node 24 / TypeScript 6 runner. Shared GitHub CI and release
runners use `benchmark:watch:ci`: they fail on deterministic leak and process
budgets while recording absolute timing measurements for comparison.

CI also defines an opt-in strict timing job. Provision a quiet Linux x64
self-hosted runner with the `jaunt-ts-performance` label, calibrate the checked-in
baseline on that machine, and set the repository variable
`JAUNT_TS_STRICT_BENCHMARK_ENABLED=true`. CI will then run the strict command on
every push and same-repository pull request with Node 24.14.0. Pull requests from
forks never reach the self-hosted runner. When the variable is absent, the job is
skipped before runner assignment, so repositories without that dedicated machine
do not accumulate queued jobs.

This is an alpha published under the `next` dist-tag. It supports project-reference
builds, cross-module generated dependencies, concrete class inheritance, strict
class adapters, and `@jauntPreserve` bodies. A preserve tag belongs on the one
concrete implementation of a non-overloaded method or accessor. Preserved code may
use parameters, `this`, local bindings, standard globals, and runtime imports from
the paired context; other runtime imports are rejected.

The worker still rejects abstract governed classes, authored `private`/`protected`
members, parameter properties, computed member names, mixin or `implements`
heritage, and preserve tags on overload groups. `.mts`, `.cts`, and JavaScript specs
are not supported.

## License

MIT. See [LICENSE](./LICENSE).
