# Jaunt-for-TypeScript — implementation plan

**Design:** [`DESIGN.md`](DESIGN.md) (read first).
**Status:** execution plan for the validated architectural spike at PR 79 commit
`a2132b4`. The plan includes the corrections from the second design review.
**Implementation strategy:** keep the Python Jaunt orchestration core and add a
project-local Node/TypeScript worker. The first release is one product, one CLI,
one `jaunt.toml`, and one `jaunt check` gate.

## 1. Outcome

The finished feature lets a TypeScript project write private `*.jaunt.ts` or
`*.jaunt.tsx` spec inputs and consume ordinary generated TypeScript through a
normal public module:

```text
src/tokens/index.jaunt.ts    authored contracts and stubs; never executed
src/tokens/index.context.ts  handwritten leaf dependencies
src/tokens/index.ts          committed public facade
src/tokens/__generated__/
  index.api.ts               deterministic declaration mirror; no runtime code
  index.ts                   generated implementation
```

`jaunt build` discovers and digests the spec without executing it, asks Codex
for an implementation, validates the candidate against the owning TypeScript
project in memory, and commits a recoverable artifact transaction only after the
affected project is green. `jaunt test`, contract mode, status/check, watch, daemon jobs, and
ejection then build on that same target abstraction.

The production path uses ordinary TypeScript imports. It has no loader hook and
no Jaunt runtime dependency below the public facade.

## 2. Scope and compatibility

### Included in the first stable release

- `.ts` and `.tsx` magic specs; functions, async functions, concrete classes,
  methods, accessors, generics, and overloads.
- ESM and CommonJS projects expressed through ordinary `.ts`/`.tsx` files and
  the owning `tsconfig`. Explicit `.mts`, `.cts`, JavaScript, and JSX-in-JS specs
  are deferred.
- One config covering Python, TypeScript, or a mixed workspace.
- TypeScript project references, package workspaces, path aliases, and separate
  production/test TypeScript projects.
- Magic build/test, contract adopt/reconcile/check/eject, `jaunt design`, status,
  clean/orphans, watch, daemon jobs, and JSON output.
- Vitest as the sole test runner and fast-check for generated properties.
- The existing Codex engine and model policy. TypeScript generation uses
  `gpt-5.6-sol`; semantic-gate judgment uses `gpt-5.6-luna`.

### Compatibility rules

- Existing version-1 Python-only `jaunt.toml` files retain their current meaning
  and output byte-for-byte unless a Python feature itself changes.
- Config version 2 introduces language targets. A distinct
  `jaunt migrate --config-v2` action produces the new shape without changing Python
  routes or artifacts. `--merge-projects` keeps its existing consolidation meaning.
- The Node version used to run the analyzer is a tool-host requirement, not a
  requirement on the generated program's deployment runtime. Freeze the exact
  host range when the worker package is created and test it independently from
  target runtimes.
- Generated TypeScript is committed/reviewed just like Python generated output.
  A missing implementation is always a deterministic `jaunt check` failure.
  Before `jaunt sync` it is also a compiler failure; after sync, a provenance-marked
  throwing placeholder restores project/editor typing without counting as built.

### Deferred

- Runtime registry/proxy substitution and correctness-critical loader hooks.
- Jest, Mocha, or framework-specific test-runner adapters.
- `.mts`, `.cts`, JavaScript specs, and declaration-only JavaScript consumers.
- Abstract governed classes and authored nominal private/protected members.
- Model-invented public APIs at build time. Public API invention happens only
  through the reviewable `jaunt design` declaration-patch flow.

## 3. Fixed architectural decisions

1. **The Python core remains the orchestrator.** It owns CLI parsing, Codex,
   scheduling, cost, progress, journals, artifact writes, watch, and daemon jobs.
2. **A Node worker owns TypeScript semantics.** It owns config/project parsing,
   module resolution, ASTs, checker queries, contract IR, conformance, and
   project-overlay validation.
3. **Analysis never executes user code.** Vitest, Vite config, test setup, and
   mutation runs use disposable subprocesses separate from the analyzer.
4. **Spec files are private inputs.** Runtime value imports of `*.jaunt.ts[x]`
   are errors outside spec/test-spec inputs. Type-only spec imports are allowed
   only inside analysis inputs; production source types against the API mirror.
5. **Mapping is name-preserving.** `foo.jaunt.ts` maps to public `foo.ts` and
   generated `__generated__/foo.ts`; `index.jaunt.ts` maps to `index.ts` and
   `__generated__/index.ts`. Two specs can coexist in one directory.
6. **Context is a one-way leaf.** `foo.context.ts` may be imported by the
   generated implementation, but it may not value-import `foo`, its generated
   implementation, or any spec file. Handwritten code that consumes the generated
   API belongs in an ordinary downstream module that imports the public facade; the
   same facade does not re-export that downstream consumer.
7. **The public facade is ordinary committed source.** It uses normal exports;
   no tool must be present at runtime.
8. **A deterministic API mirror separates authoring from publication.** Jaunt
   renders `__generated__/foo.api.ts` from contract IR. Facades, context, and
   implementations type against this declaration-only module, so normal emit never
   includes executable spec stubs and published declarations never reference a
   private `*.jaunt.ts` path.
9. **Export annotations pin consumer types, but do not constitute the full
   validator.** A Jaunt semantic conformance pass handles class variance,
   overloads, generics, modifiers, escape hatches, and the exact export set.
10. **Default initializers are forbidden in governed declarations in v1.** Use an
   optional parameter plus TSDoc to state default behavior. This avoids claiming
   a runtime default that an erased spec cannot enforce mechanically.
11. **Digests use Jaunt-owned semantic IR.** They do not hash printer output or
    leaf type strings. Referenced public types participate transitively.
12. **Project-local TypeScript is required.** The worker resolves the compiler from
    the owning workspace, checks it against a tested compatibility range, and
    fingerprints the exact version and effective options. A missing compiler is an
    exit-2 install diagnostic; the worker never silently switches compilers.
13. **Optional resolver hooks remain development sugar only.** No build, test,
    check, publish, or consumer path depends on them.
14. **Deterministic synchronization precedes paid generation.** `jaunt sync`
    renders API mirrors and, for unbuilt modules, exact typed throwing placeholders.
    It never calls a model or turns an unbuilt module fresh.

## 4. Authoring contract

### 4.1 File mapping

For a spec at `<dir>/<stem>.jaunt.ts[x]`:

| Role | Path |
|---|---|
| Spec input | `<dir>/<stem>.jaunt.ts[x]` |
| Public facade | `<dir>/<stem>.ts` |
| Deterministic API mirror | `<dir>/__generated__/<stem>.api.ts` |
| Generated implementation | `<dir>/__generated__/<stem>.ts[x]` |
| Leaf runtime context | `<dir>/<stem>.context.ts[x]` (optional) |
| Generated test tiers | configured test output, preserving the spec-relative stem |

The worker derives this mapping; it is not a manifest maintained by hand. It
fails before generation on collisions, a missing facade, a generated path that
escapes its source root, or two target projects claiming different output paths.

The facade template uses runtime-compatible specifiers, normally `.js` under
Node-style resolution, so normal `tsc` emit does not require importing `.ts`
extensions:

```ts
export type * from "./__generated__/index.api.js";
export * from "./index.context.js";
export * from "./__generated__/index.js";
```

Absent optional files are omitted. `jaunt init`, `jaunt sync`, and `jaunt migrate`
can create a missing canonical facade, but none silently overwrites a user-written
facade.

### 4.2 Spec grammar

A spec imports markers from the TypeScript package and opts in once:

```ts
import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/** Convert a title to a stable URL slug. */
export function slugify(title: string): string {
  return jaunt.magic();
}
```

Normative rules:

- `magicModule()` must be one statically resolvable top-level call. Its options
  are literals and merge into governed declarations key by key.
- A governed function body is exactly `return jaunt.magic(options?)`; a `void`
  function may contain the bare call. Async functions use the same form.
- An overload group is contiguous: one contract TSDoc block precedes the first
  exported overload signature, followed by any additional bodyless overloads and
  exactly one canonical stub implementation. Per-overload bodies/docs are rejected.
- Class constructors, methods, and accessors use a single `jaunt.magic()` call.
- `deps` contains statically resolved identifiers. `prompt` is a string literal.
- Interfaces and type aliases are type context. Runtime enums, namespaces,
  initializers, static blocks, and arbitrary executable declarations are rejected.
- A real method explicitly copied into generated output uses `@jauntPreserve`.
  Its value references are limited to parameters, `this`, standard globals, and
  imports from the paired context; the worker reproduces those safe imports. Other
  spec-local/runtime import closure is rejected.
- Non-Jaunt decorators on governed declarations are rejected in v1. Parameter
  initializers and authored private/protected members are also rejected.
- `any` is rejected anywhere in an authored governed public boundary.
- Public TSDoc and declarations are copied into the deterministic API mirror so
  editor hovers and emitted declarations retain the contract.
- Direct value imports of spec modules are allowed only from other spec inputs
  and `*.jaunt-test.ts[x]` files, which are themselves never executed.

### 4.3 Test specs and contract markers

- Authored test intent uses `<name>.jaunt-test.ts[x]`. Its functions contain only
  `jaunt.testSpec({ targets: [...] })` and are excluded from Vitest collection.
- Generated tests import the public facade, never a spec or generated-private path.
- Real code enters contract mode through `@jauntContract` TSDoc. `@example` and
  `@throws` are the supported documentation tags; `@fixtures` and `@prop` are
  Jaunt extensions parsed under an explicit TSDoc configuration.
- A property arbitrary is rendered into a typed declaration before execution:
  `const arb: fc.Arbitrary<ExpectedType> = expression`. `any` is rejected.

## 5. Runtime and process architecture

```text
                    persistent for one CLI/watch job
Python CLI/core  <------------------------------------>  Node analyzer
  config/targets         versioned JSONL protocol          TS Programs
  Codex calls                                               IR/checker
  scheduling                 candidate overlays             conformance
  manifested writes                                        diagnostics
       |
       +-----------------> disposable Node subprocesses
                             Vitest + user config
                             held-out reporter
                             mutation workers
```

The analyzer process is pure with respect to user execution. It may read source,
`tsconfig` JSON, `package.json`, lockfiles, and compiler libraries. It never
imports application modules, Vite config, test setup, or package scripts.

Each ordinary CLI invocation starts one analyzer session per compatible TypeScript
compiler/project-reference graph and closes it before returning. Most workspaces have
one. Unrelated package owners may use different supported compiler versions, but one
reference graph may not mix them. Watch keeps those sessions and invalidates changed
files/configs. Daemon jobs never share analyzers across worktrees or repositories.

## 6. Repository changes

### 6.1 New npm package

The public package coordinate is `@usejaunt/ts`. The `usejaunt` npm organization
is owned by the Jaunt maintainer, and `0.0.0-alpha.0` bootstraps the coordinate with
the typed marker API and its ESM/CommonJS runtime guard. Expand that tracked package
at `packages/jaunt-ts/` into the following self-contained layout:

```text
packages/jaunt-ts/
  package.json
  package-lock.json
  README.md
  index.{js,cjs,d.ts}              published alpha bootstrap; replaced by build output
  tsconfig.json
  src/
    spec.ts                       marker/runtime types
    protocol/{messages,errors}.ts
    worker/{main,server,session}.ts
    analyzer/
      config.ts                   tsconfig/reference parsing
      projects.ts                 project graph and file ownership
      overlay.ts                  in-memory filesystem and solution builds
      discovery.ts                spec/test/contract discovery
      docs.ts                     TSDoc cleaning and tag parsing
      ir.ts                       semantic IR extraction
      type_graph.ts               referenced-type Merkle graph
      dependencies.ts             spec and import dependency edges
      conformance.ts              free-function/module checks
      class_conformance.ts        strict per-member class checks
      provenance.ts               package dependency audit
      artifacts.ts                path mapping and orphan discovery
      diagnostics.ts              canonical diagnostic records
    test/
      runner.ts                   disposable Vitest entry point
      reporter.ts                 tiered JSON reporter/redactor input
      properties.ts               typed fast-check rendering
      mutation.ts                 disposable mutation worker coordinator
```

The npm package exports `@usejaunt/ts/spec`, the worker entry point, and the
disposable test-runner entry point. It does not expose analyzer internals as a
public API in v1.

The name is deliberate. Unscoped `jaunt` is an unrelated object-path package,
and the `@jaunt` organization belongs to an unrelated npm account. npm also
rejected unscoped `jaunt-ts` under its package-name similarity guard. The
`@usejaunt/ts` coordinate keeps the product name recognizable without implying
control of those existing names, and the scope leaves room for real future
packages such as `@usejaunt/cli`. Do not publish `@usejaunt/jaunt` or other empty
placeholders merely to reserve them; add a package only when it has a genuine role.

### 6.2 New Python modules

Create:

```text
src/jaunt/targets/
  __init__.py
  base.py                         target IDs, units, reports, adapter protocol
  orchestrator.py                 multi-target aggregation and exit precedence
  python.py                       wrapper around existing Python services
  typescript.py                   adapter delegating to jaunt.typescript
src/jaunt/typescript/
  __init__.py
  config.py                       version-2 target config dataclasses
  protocol.py                     typed JSONL request/response models
  worker.py                       lifecycle, timeout, cancellation, stderr
  workspace.py                    Python view of worker project/routes
  artifacts.py                    headers, sidecars, atomic write plans
  builder.py                      TS build transaction
  tester.py                       test generation and Vitest orchestration
  contracts.py                    adopt/reconcile/check/eject integration
  design.py                       declaration-patch flow
  prompts/
    build_system.md
    build_module.md
    test_system.md
    test_module.md
    design_system.md
    design_user.md
```

Modify, without replacing the Python behavior in place:

- `config.py`: version-2 target parsing and version-1 compatibility adapter.
- `generate/base.py` and `generate/codex_backend.py`: language/path-aware generation
  context and TypeScript prompt selection.
- `cli.py`: dispatch, language-prefixed targets, merged human/JSON reports, and new
  `design` command.
- `status_core.py` and `reconcile.py` remain Python implementations called by the
  Python adapter. Target orchestration aggregates their reports elsewhere.
- `journal.py`, `cost.py`, progress, and cache remain language-neutral services;
  records/cache namespaces carry qualified target IDs without target-specific
  branching in those modules.
- `init_template.py`, `agent_docs.py`, instructions, and repo-map code: TypeScript
  scaffolding and context.
- `watcher.py`, `daemon.py`, `jobs.py`, and `landing.py`: language-neutral job IDs
  and affected-target scheduling.

Do not make `builder.py`, `tester.py`, `status_core.py`, `reconcile.py`, or Python
discovery understand TypeScript ASTs. Python behavior stays in place behind its
adapter; TypeScript behavior stays in `jaunt.typescript` and the Node package.

## 7. Configuration and identity

### 7.1 Version-2 config

```toml
version = 2

[target.py]
source_roots = ["services/api/src"]
test_roots = ["services/api/tests"]
generated_dir = "__generated__"
infer_deps = true
test_infer_deps = true
emit_stubs = true
ty_retry_attempts = 1
async_runner = "asyncio"
check_generated_imports = true
generated_import_allowlist = []
pytest_args = ["-q"]
auto_class_tests = false
contract_battery_dir = "tests/contract"

[target.ts]
source_roots = ["packages/*/src"]
test_roots = ["packages/*/tests"]
projects = ["tsconfig.json"]
test_projects = ["tsconfig.test.json"]
tool_owner = "." # package that directly owns @usejaunt/ts + typescript
generated_dir = "__generated__"
test_runner = "vitest"
vitest_config = ""
vitest_args = []
auto_class_tests = false
fast_check_runs = 50
contract_battery_dir = "tests/contract"
```

`projects` entries may be solution configs, leaf configs, or globs. The worker
expands `extends` and `references` through TypeScript APIs. `test_projects` is
optional; when absent, tests must belong to a production project or discovery
fails with an actionable configuration error.

No "nearest tsconfig" heuristic is used. Every spec, facade, generated file, and
test must belong to an explicit project role. Each spec/facade/output has exactly one
production owner; a test project may consume it but cannot co-own its generated
artifact. Multiple production claims are always an ambiguity error.

Private spec/test-spec files must be excluded from production/test emit. A spec is
owned by the compilation unit that contains its derived public facade; a test spec is
owned by the project that contains its derived generated-test path. The worker then
adds the private input to a non-emitting analysis Program with that owner's effective
options. This rule also makes ownership decidable on the first build.

The nearest containing `package.json` inside the config root owns dependency
provenance. Compilation ownership and package ownership remain separate fields.

`tool_owner` resolves exactly one installed worker package and TypeScript compiler
identity for the entire TypeScript target. It is independent of per-file dependency
owners. Every configured reference graph must resolve that same identity in v1;
mixed identities are rejected. Supporting several tool owners in one Jaunt config
requires a future named/multiple-target schema rather than an implicit nearest-package
rule.

In version 2, top-level `[build]` and `[test]` retain only shared concurrency and
instruction policy. Python-only generation/test keys move into `[target.py]`; TS-only
keys live in `[target.ts]`. Shared `[codex]`, `[semantic_gate]`, `[daemon]`,
`[contract]`, `[context]`, and `[skills]` remain top-level. Prompt overrides become
`[prompts.py]` and `[prompts.ts]`. Version-1 accessors map the old sections exactly,
and a version-2 file may not also contain `[paths]`.

### 7.2 Stable IDs

- Python IDs remain unchanged for backward-compatible CLI output.
- TypeScript module ID:
  `ts:<root-relative-spec-path-without-.jaunt.ts[x]>`.
- TypeScript symbol ID appends `#<export-path>`.
- Cross-language internal reports always carry `{language, module_id, symbol_id,
  owner, project}` rather than parsing display strings.
- CLI `--target ts:packages/web/src/token#createToken` selects a TS symbol;
  unprefixed dotted targets retain their Python meaning.

Paths serialized into digests and protocol records are root-relative POSIX paths.
Absolute paths never participate in freshness.

## 8. Worker protocol

Use newline-delimited JSON on stdout. Stderr is diagnostic logging only.

Every request is `{protocol, id, method, params}`. Every response is either
`{protocol, id, ok: true, result}` or `{protocol, id, ok: false, error}`. Errors
contain a stable code, message, retryable flag, and structured diagnostics.

### Required methods

1. `initialize`: root, configured projects, target paths, client/tool versions;
   returns worker, protocol, TypeScript, package-manager, capabilities, a session ID,
   and monotonic workspace epoch/input snapshot.
2. `analyzeWorkspace`: project/reference graph, routes, spec/test/contract records,
   dependency graph, import violations, and baseline diagnostics.
3. `analyzeContracts`: semantic IR, prose/structure digests, type graph, API digests,
   deterministic API-mirror source, and exact dependency edges for selected modules.
4. `validateOverlay`: reserved-binding candidate sources plus selected modules;
   deterministically composes API mirrors/public boundaries and returns the exact
   artifact bytes/content hashes, project diagnostics, semantic conformance,
   export-set/provenance checks, and affected dependent projects without writing.
5. `findOrphans`: expected/actual TypeScript artifacts for clean/check.
6. `invalidate`: changed paths; refreshes affected Programs and advances the epoch.
7. `shutdown` and `cancel`: graceful completion and queued-request cancellation.

Protocol requirements:

- A handshake mismatch fails before discovery with install/upgrade guidance.
- Requests have deadlines; Python terminates a hung worker process group.
- Messages and overlays have explicit size limits.
- Responses are deterministic: sorted paths, symbols, edges, and diagnostics.
- The worker confines all file reads and overlay writes to approved roots except
  compiler/package resolution through the package manager's installed tree.
- User source and generated test details never appear in unredacted debug logs by
  default.
- Golden request/response fixtures are consumed by both Python and TypeScript tests.

`validateOverlay` returns the session ID/epoch/input snapshot alongside exact bytes
and hashes. Python commits only while they still match. TypeScript compiler work is
synchronous inside a session: in-band cancel can drop queued work, while an active
hung compile is cancelled by terminating and recreating that analyzer process.

## 9. Project graph and overlay validation

The worker builds one incremental Program per referenced config that has root files.
A config may own files and also reference dependencies; solution-only configs remain
graph nodes without Programs. It parses the reference DAG and validates affected
compilation units in topological order.

For a candidate build:

1. Parse specs in a non-emitting analysis Program using the owning project's
   effective options, even when production `exclude` omits `*.jaunt.ts[x]`.
2. For preflight, add the deterministic API mirror plus a typed throwing placeholder
   for each exact missing owned implementation. This suppresses only expected
   first-build module diagnostics.
3. Replace placeholders with missing or changed implementation candidates in the
   production-project overlay.
4. Rebuild ordinary project source with its effective compiler options, and compile
   candidate/virtual conformance files under Jaunt's stronger public-boundary profile.
5. Capture declaration outputs in memory for referenced downstream projects.
6. Revalidate dependents whose consumed API digest changed.
7. Return all diagnostics and proposed API digests to Python.

This makes first build possible even though the committed facade imports a missing
implementation. Nothing touches disk until the complete affected project closure is
valid. Multi-module builds validate one combined overlay and commit the batch
through a recoverable manifest transaction; each file replacement is atomic, and a
crash-partial transaction is detected as stale on the next command.

Mandatory checks do not weaken project options or retroactively apply stronger flags
to unrelated user/third-party source. Candidate files and the synthetic conformance
project always use strict null/function checks, no implicit `any`, exact optional
properties, and suppression scanning. The owner project still runs under its native
effective options, so pre-existing non-strict code is not blamed on a candidate.

Every production/test emitting config must exclude `*.jaunt.ts[x]` and
`*.jaunt-test.ts[x]` from its root files. The worker verifies this and fails preflight
with an exact config fix; private inputs enter only non-emitting analysis Programs.

## 10. Semantic IR and freshness

### 10.1 IR records

Each governed symbol produces a versioned record with:

- stable symbol ID, declaration kind, exported name, modifiers, and type parameters;
- recursively normalized parameter, return, member, constructor, accessor, and
  overload syntax;
- explicit spec dependencies and referenced type-symbol IDs;
- normalized `@jauntPreserve` bodies;
- cleaned TSDoc and Jaunt options, separated into prose and structure;
- facade/context/generated paths and owning project/package IDs.

Type nodes are serialized by a Jaunt-owned AST visitor. Raw source slices and
`checker.typeToString()` are not digest inputs. Union/intersection members,
properties, type parameters, constraints, mapped/conditional types, and import type
references have explicit representations. Union members are sorted by canonical
child digest. Intersection members preserve source order in v1 because callable or
constructable intersections can behave like ordered overload sets. Overloads and
tuple elements likewise retain source order where it affects resolution or meaning.

### 10.2 Transitive type graph

Changing an interface behind a named return type must stale its consumers. Build a
Merkle graph of exported type declarations:

1. Hash each declaration's local canonical payload and referenced symbol IDs.
2. Collapse recursive strongly connected components.
3. Hash each component from its sorted local members plus outgoing component hashes.
4. Include the resulting referenced-type roots in every spec structural digest and
   module API digest.

The same graph crosses project references through emitted-declaration/source
metadata. A referenced `.d.ts` symbol maps back to its workspace spec ID when Jaunt
metadata exists; third-party declarations contribute a package/version/file digest.

### 10.3 Freshness classes

- `structural`: implementation rebuild, including transitive type/API changes.
- `prose`: semantic gate, then refreeze or rebuild.
- `fingerprint`: deterministic revalidation/re-stamp when compiler, options,
  prompts, worker, or IR scheme changes but contract IR does not.
- `unbuilt`: missing implementation or sidecar.
- `invalid`: generated code, facade, provenance, or project diagnostics fail.
- `orphan`: artifact has no remaining spec/test/contract owner.

Context structure is a dependency API input and triggers dependent implementation
rebuilds. Context behavioral TSDoc follows the prose semantic-gate path. Only
body-only context edits trigger project/test revalidation without a paid model
rebuild. Facade/downstream edits likewise revalidate their project without restaling
an unchanged implementation contract.

Headers carry tool/worker/compiler/IR versions and local/module/API digests. A
sibling committed JSON sidecar carries canonical per-symbol IR snapshots and
dependency hashes. `jaunt check` remains reproducible from committed files when
ephemeral `.jaunt/` caches are absent.

## 11. Conformance and safety checks

The generated implementation imports types from its deterministic API mirror for
pinning, and the worker adds a synthetic, non-emitted conformance module. Production
source never imports the private spec, even in a type position.

Codex does not author the public export boundary. It returns reserved internal
bindings such as `__jaunt_impl_createToken` and `__jaunt_impl_TokenStore`. The worker
parses those bindings, rejects model-authored exports, and deterministically appends
the TSDoc-bearing, API-mirror-typed public exports. This prevents a model from hiding
an unsafe cast in the boundary itself and makes final source composition testable
without a model.

Validation has two independent proofs: the API mirror is compared against symbols
from the original non-emitting spec Program, and raw implementation bindings are
checked against those original authored symbols. Checking implementation only
against the mirror is insufficient because an IR/renderer defect could otherwise
make both sides wrong in the same way.

### Free functions

- Check every authored overload and generic constraint against the implementation.
- Reject missing exports, duplicate exports, and any extra public export not declared
  by the spec.
- Ban explicit/implicit `any`, `@ts-ignore`, `@ts-expect-error`, `@ts-nocheck`, and
  boundary double assertions. Internal `unknown` plus checked narrowing is allowed.
- Scope the ban to explicit `any` in candidate source, implicit-any diagnostics
  originating in candidate/virtual files, and recursively resolved `any` at reserved
  implementation boundaries. Unrelated baseline or third-party `any` is not blamed
  on the candidate.
- Copy authored TSDoc to the pinned public export.

### Classes

Do not rely on `typeof Impl satisfies typeof Spec`: TypeScript methods and
constructors are bivariant even under `strictFunctionTypes`.

- Generate one virtual adapter for every authored constructor, method overload,
  accessor side, and generic environment. The adapter accepts authored parameters,
  calls the raw reserved implementation binding, and carries the authored return
  type. The call forces contravariant input checking outside TypeScript's
  method/constructor bivariance exemption; the return annotation checks covariance.
- Never fall back to class assignability when an authored construct cannot be
  represented by an adapter. Report it as unsupported at discovery.
- Compare getters, setters, fields, optionality, readonly, static, and
  accessibility modifiers explicitly.
- Check overloads and generic constraints per member.
- Reject authored TypeScript `private`/`protected` state in v1 because a separately
  generated class cannot satisfy its nominal identity. Generated `#private` state is
  allowed.
- Verify the value export and instance type alias separately.

Conceptually, a method check is:

```ts
function __check_put(
  impl: __jaunt_impl_TokenStore,
  subject: string,
  token: string,
  exp: number,
): void {
  return impl.put(subject, token, exp);
}
```

An implementation narrowed to `subject: "only"` fails at the call even though a
whole-class assignment would pass.

### Imports and runtime graph

- Ban runtime imports of spec/test-spec files.
- Enforce the context leaf rule and reject cycles containing
  `facade -> generated -> context -> facade` before generation.
- Generated code may import declared leaf context and other public facades, never
  another module's generated-private path unless Jaunt emitted that edge explicitly.
- Audit resolved package imports against the owning package's dependencies,
  peer/optional dependencies, workspace packages, builtins, and package import maps.
  Test-only imports may use dev dependencies; production generated code may not.

## 12. Build transaction

For each `jaunt build` invocation:

1. Load config and start/handshake with the analyzer.
2. Analyze all configured targets; fail before model calls on routes, compiler,
   facade, import, or project errors. Suppress only diagnostics for the exact missing
   Jaunt artifacts that the pending first-build overlay will provide; every other
   baseline project diagnostic remains an exit-2 error.
3. Compute IR, dependency/API graph, stale reasons, and target closure.
4. Run the prose semantic gate where eligible. Re-stamp deterministic cases.
5. Build language-specific prompt context: spec source, canonical contract IR,
   leaf context, dependency public APIs, compiler/module settings, expected exports,
   attached test intent, and repository context.
6. Ask Codex for implementation source only. The prompt forbids spec imports,
   extra exports, `any`, suppressions, and edits outside the requested candidate.
7. Validate the candidate through `validateOverlay`. Feed canonical diagnostics back
   to the existing bounded repair loop.
8. When all selected candidates and affected projects pass, write API mirrors,
   generated implementations, headers, sidecars, and journal records using temp
   files plus atomic replacement.
9. Emit merged Python/TypeScript human or JSON reports with usage and actual cost.

If any candidate in a project batch fails, none of that batch is written. Unrelated
owner-project batches may complete independently, matching today's per-owner Python
scheduling.

Discovery, semantic gating, and freshness classification prepare artifacts but do not
rewrite headers immediately. Paid candidates, deterministic API mirrors, refreezes,
and re-stamps join one validated owner-project transaction after dependent expansion.
Only then does Python commit the prepared manifest. A candidate/dependent failure
leaves implementations, API mirrors, headers, and sidecars unchanged. Existing
Python builds retain their current per-module commit behavior.

## 13. Test generation and execution

### 13.1 Generated test transaction

`jaunt test` follows the build transaction through discovery, IR, prompt generation,
overlay typechecking, and a recoverable manifested write transaction. Test generation
is a separate Codex role and cannot inspect implementation-generation conversation
state.

1. Resolve authored `*.jaunt-test.ts[x]` specs and opt-in automatic class tests.
2. Generate example-tier tests from explicit authored cases and derived-tier tests
   from the contract/TSDoc.
3. Render tests against public facades and the typed fixture surface.
4. Typecheck the battery in its configured test project before running anything.
5. Invoke the disposable test-runner entry point with an explicit file list,
   non-watch mode, bounded timeout, and Jaunt reporter.
6. Convert the reporter result into the existing report and repair context. Retry
   implementation or test generation through the current bounded loops.

Vitest config and setup files may execute only in the disposable runner. Analyzer,
status, check discovery, and build never load them.

### 13.2 Held-out barrier

- Example-tier files use `.example.test.ts[x]` and keep normal failure detail.
- Derived-tier files use `.derived.test.ts[x]`. Repair feedback is constructed from
  an allowlisted DTO containing only a synthetic opaque case ID and normalized
  exception category enum; arbitrary `error.constructor.name` is not trusted.
- Tier classification requires both the filename and a valid Jaunt provenance
  header; unknown/unmarked failures default to derived.
- Collection/config/setup/hook failures default to derived because their text may
  contain held-out values.
- Repair-mode child stdout/stderr is always captured, never inherited. Jaunt disables
  user/custom reporters and locks reporter/include/timeout/non-watch settings for the
  protected run, so setup/config output cannot bypass the Jaunt reporter.
- A leak assertion checks messages, stacks, diffs, snapshots, console output,
  warnings, aggregate causes, serialized errors, and captured process output as a
  second defense; redaction is not the primary construction mechanism.
- Human debug output is separate. `--no-redact-derived` is a loud opt-out and never
  changes default JSON output silently.
- Derived sources used by an implementation repair live in a temporary runner
  directory and are removed before Codex receives feedback. They are never added to
  the implementation model's readable workspace. Example-tier and committed
  contract batteries remain reviewable project artifacts.

### 13.3 Fixtures and properties

- `tests/fixtures.ts` exports the canonical `test.extend` value for each test owner.
- The renderer emits a typed fixture destructure; missing fixtures fail before
  Vitest starts.
- Fixture public API digests participate in dependent battery freshness.
- Each `@prop` arbitrary is parsed as an expression, inserted into
  `const arb: fc.Arbitrary<T> = ...`, checked for `any`, and then passed to
  `fc.property` or `fc.asyncProperty`.
- Seed, run count, fast-check/Vitest/Node versions, reporter protocol, protected
  effective settings, expected parameter type, and case digest are battery
  fingerprint inputs. Replay data is available to humans while repair feedback
  remains redacted.

### 13.4 Mutation strength

Mutation uses one disposable subprocess/process group per mutant (a disposable batch
coordinator may bound and schedule them). Each compiling mutant gets fresh module
state. A per-mutant timeout kills its full process group, including descendants; a
global timeout kills the coordinator and all remaining groups. This covers sync
loops, unresolved promises, open handles, child processes, worker crashes, and
explicit exits.

Only compiling mutants enter the denominator. Mutant order, concurrency, cases, and
scores are deterministic and recorded in battery metadata.

## 14. Contract mode and lifecycle commands

### 14.1 `jaunt design`

Use `@jauntDesign` on a doc-only declaration in a private spec.

- Default behavior calls the model and prints a unified declaration/TSDoc patch; it
  does not write.
- `--apply` checks the source digest and dirty-tree policy, applies atomically,
  removes the marker, and validates the analysis overlay. It leaves the module
  unbuilt so API and implementation remain separate review steps.
- The model may change only the marked declaration and its associated TSDoc/type
  imports. The worker rejects executable bodies and unrelated edits.
- JSON includes target ID, patch, applied flag, diagnostics, usage, and actual cost.
- Re-running after apply is idempotent: there is no remaining design marker.

### 14.2 Contract mode

- `jaunt adopt` inserts `@jauntContract` without changing executable code and derives
  a proposed battery.
- `jaunt reconcile` refreshes cases through the typed overlay and disposable Vitest
  runner. Old committed batteries remain byte-identical on failure.
- `jaunt check` verifies source/battery digests, compiles affected test projects,
  runs the deterministic battery, checks strength metadata, and makes no model call.
- `jaunt eject` removes the tag/provenance while leaving ordinary implementation and
  Vitest tests.
- Text surgery preserves shebangs, directives, comments, overload groups, import
  ordering, and line endings; ambiguous syntax is refused rather than reformatted.

### 14.3 Magic ejection

Magic ejection operates on a whole TypeScript spec module in v1:

1. Require a fresh implementation, API mirror, facade, and green owner project.
2. Move generated implementation and public declarations to ordinary non-generated
   paths, removing provenance and reserved implementation names.
3. Retarget context/facade imports.
4. Remove the private spec, generated artifacts, and sidecars.
5. Validate compile, declaration emit, package output, and tests in an overlay before
   committing the transaction.

The result contains no Jaunt import, `.jaunt` path, generated-directory dependency,
or provenance header. Ejection refuses layouts it cannot update without guessing.

### 14.4 Deterministic sync

- `jaunt sync [--target ...]` parses the selected dependency closure and writes API
  mirrors without calling Codex or the semantic gate.
- For a new module it creates the canonical facade only when that path is absent and
  emits a self-contained, exact-export typed throwing placeholder when no real
  implementation exists. Existing handwritten facades and real generated
  implementations are never replaced.
- Placeholder headers carry `state = "unbuilt"`; `status` and `check` remain blocked,
  while the owner project, context layer, editor hovers, and declaration references can
  typecheck before a paid build.
- Mirror, facade, placeholder, sidecar, and journal writes use the same prevalidated,
  recoverable owner-project transaction as build. JSON reports stable `mirrors`,
  `placeholders`, `created_facades`, and `failed` records.

### 14.5 Init, migrate, clean, and orphans

- `jaunt init --language ts` scaffolds config, spec/context/facade/test examples, and
  prints the package-manager-specific dev-dependency command. It mutates
  `package.json` only under an explicit install/apply flag.
- `jaunt migrate` remains plan-only by default. It covers config v1 -> v2, old preview
  imports/layout, digest/protocol re-stamps, facade repair, and API-mirror emission.
- Every action is labeled deterministic rewrite, free re-stamp, model rebuild, or
  manual intervention.
- `jaunt clean --orphans` removes implementation, API mirror, generated battery, and
  sidecars only when ownership is absent; dry-run and journal behavior match Python.

## 15. Core integration and CLI contracts

### 15.1 Target adapter boundary

Do not add TypeScript conditionals to Python AST modules. Introduce an adapter used by
CLI/status/watch/daemon:

```python
class TargetAdapter(Protocol):
    language: str
    async def discover(...) -> TargetWorkspace: ...
    async def status(...) -> TargetStatus: ...
    async def build(...) -> TargetBuildReport: ...
    async def test(...) -> TargetTestReport: ...
    async def check(...) -> TargetCheckReport: ...
    async def find_orphans(...) -> tuple[TargetArtifact, ...]: ...
```

Wrap the existing Python functions with `PythonTargetAdapter` before moving behavior.
Golden tests freeze version-1 Python CLI/JSON output. The TypeScript adapter delegates
semantic work to the worker and reuses common Codex, progress, cost, journal, cache,
and scheduling services.

Keep the current Python critical-path scheduler inside `builder.run_build()` and give
the TS adapter its own owner-project batch scheduler. The outer orchestrator runs the
two adapters under one Codex semaphore and shared cost/cache/progress services; it
does not build a cross-language DAG. Cross-language spec dependency edges are
unsupported in v1.

Replace the Python-shaped backend entry with a generic request while preserving the
existing Python adapter:

```python
@dataclass(frozen=True)
class GenerationRequest:
    language: str
    kind: str
    target_path: str
    context_files: dict[str, str]
    prompt: str
    cache_payload: dict[str, object]
    validator: CandidateValidator
```

The backend only creates the temporary workspace, invokes Codex, reads the requested
artifact, and reports usage/advisories. It does not unconditionally call Python's
`validate_generated_source`, choose `.py`, or hash `ModuleSpecContext`. The Python
adapter supplies its current prompt/cache payload/validator unchanged; TS supplies
reserved-binding output and validates through `validateOverlay`.

### 15.2 CLI behavior

- `build`, `test`, `status`, `specs`, `check`, `clean`, `watch`, daemon/jobs, and
  instructions operate on every configured target by default. `sync` does as well,
  but performs only deterministic TypeScript artifact preparation in v1.
- `--language py|ts` narrows a command. Unprefixed Python `--target` values remain
  valid; TypeScript uses stable `ts:` IDs.
- `design` is TypeScript-only in v1 and errors for other targets.
- Human output groups results by owner and language without interleaving progress
  lines from concurrent model calls.
- JSON arrays contain stable target IDs and records carry `language`; existing Python
  keys remain present. Per-language summaries are additive.
- Actual model cost is reported after model-calling commands. Preview output may state
  which modules are likely to call the model but never prints fixed dollar estimates.

Version-1 Python-only JSON remains byte-for-byte unchanged and gains no `targets`
field. Version 2 uses qualified top-level IDs and repeats language-local values under
`targets` for consumers that prefer partitioned data. Pin these draft shapes in
Phase 0; alpha revisions carry a schema version, while public-beta shapes freeze:

```json
{
  "command": "build",
  "ok": true,
  "generated": ["ts:packages/auth/src/token"],
  "skipped": ["py:api.models"],
  "refrozen": [],
  "failed": {},
  "targets": {
    "py": {"generated": [], "skipped": ["api.models"], "refrozen": [], "failed": {}},
    "ts": {"generated": ["packages/auth/src/token"], "skipped": [], "refrozen": [], "failed": {}}
  }
}
```

`test` uses the same generation keys plus `pytest`/`vitest` result records and
redacted failures. `status` preserves the current `fresh`, `stale`, `stale_changes`,
`digests`, `orphans`, and `contracts` keys with qualified IDs, plus matching
`targets.py`/`targets.ts` partitions. `check` preserves `blocked`, `checked`,
`orphans`, and `magic`; version 2 partitions `magic` by language:

```json
{
  "command": "check",
  "ok": false,
  "blocked": [],
  "checked": [],
  "orphans": [],
  "magic": {
    "py": {"fresh": [], "stale": {}, "unbuilt": [], "orphans": []},
    "ts": {"fresh": [], "stale": {}, "unbuilt": ["packages/auth/src/token"], "invalid": {}, "orphans": []}
  }
}
```

Errors add `{error: {code, message, diagnostics}}` while retaining `command` and
`ok: false`. `status` exits 0 with stale entries. Mixed-command precedence is config/
discovery 2, then generation 3, then test/check 4. `check --language` and `--target`
operate on the requested dependency closure; `--magic-only`/`--contracts-only` apply
after language filtering.

Exit codes remain:

| Code | TypeScript meaning |
|---|---|
| 0 | Success/fresh |
| 2 | Config, Node/worker/compiler, routing, discovery, or dependency-cycle error |
| 3 | Model generation or candidate-validation failure |
| 4 | Test failure, check drift, contract block, or daemon job failed/parked |
| 5 | Existing daemon/job wait timeout semantics |

### 15.3 Watch, daemon, and jobs

- Watch sends path invalidations to one analyzer session and schedules only affected
  spec/API/project closures.
- Changes to `tsconfig`, referenced configs, package manifests, lockfiles, worker or
  compiler versions invalidate the appropriate fingerprints.
- Daemon prescreen recognizes TypeScript markers without executing Node. Each job
  worktree starts its own analyzer and disposable runners.
- `CliRunner.probe()` reads qualified stale IDs/digests from version-2 status;
  `build()` invokes the language/target-scoped build; `gate()` runs target-scoped
  magic checking so unrelated concurrent stale targets do not fail the job.
- `JobRecord` adds backward-compatible `language="py"` plus a qualified artifact key.
  IDs, active/parked lookups, supersession, landing freshness, and notifications use
  that key.
- Build proposals include only implementation, API mirror, sidecars, allowed agent
  docs, and journal entries. Tests/batteries are absent unless a future daemon mode
  explicitly runs `jaunt test`/`reconcile`.
- Patch allowlisting uses the build plan's exact machine-owned paths, not every path
  below a directory named `__generated__`. Landing revalidates the owner project; no
  force path bypasses conformance.
- The existing global job pool and propose-only/auto-commit policies remain shared.

`JAUNT_GENERATED_DIR` remains Python runtime forwarding only. Clean, guard, watch
exclusion, daemon allowlisting, and orphan enumeration consume resolved per-target
paths and never assume one generated-directory basename for the workspace.

### 15.4 Repository context and skills

- Tree/repo-map discovery includes `.ts`/`.tsx` while excluding generated output,
  declaration emit, coverage, and package-manager stores.
- TypeScript AST descriptions and package/project ownership augment prompts; repo-map
  content remains freshness-decoupled as in Python.
- Initial TypeScript auto-skills derive from direct `package.json` dependencies only.
  Lockfile/transitive packages do not seed skills.
- `jaunt instructions` reports target languages, project owners, worker/compiler
  versions, freshness, facade rules, and the context-leaf rule.

## 16. Delivery phases

Each phase is independently reviewable. Do not expose a model-backed TypeScript build
until the route, IR, import-graph, and conformance negative fixtures are green. The
hard-coded Phase 1 tracer is the deliberate exception: it probes integration risk but
is neither generalized nor user-facing.

This is the stable 1.0 plan, not a small MVP. The advertised matrix includes project
references, mixed module systems, workspaces, lifecycle commands, daemon operation,
mutation strength, ejection, and coordinated publication; resource it as a multi-quarter
effort. The Phase 1 tracer and internal alpha are deliberately narrow risk-reduction
slices, not a smaller product commitment.

### Phase 0 — pin the initial contracts

Deliverables:

- Pin `DESIGN.md` around the name-preserving routes, API mirror, context DAG,
  class checks, transitive type closure, v1 syntax/support limits, and project graph.
- Change the preview to `index.jaunt.ts -> index.ts -> __generated__/index.ts`, use
  `.js` source specifiers, add the deterministic API mirror, and remove private spec
  imports from production source.
- Add fixtures for every second-review failure before implementation.
- Keep the owned `@usejaunt/ts` coordinate and published alpha bootstrap aligned
  with `packages/jaunt-ts/`; all preview and protocol fixtures use that import.
- Pin draft protocol-v1 and contract-IR-v1 schemas, golden fixtures, and the
  conformance matrix. Every alpha revision bumps its schema/digest identifier and
  produces an explicit rebuild diagnostic; compatibility freezes at public beta.

Exit gate: the design, schemas, and fixture expectations receive review; preview
typecheck/test, declaration emit, QuickInfo, and packed consumer all pass without a
resolver or raw spec artifact. The demo compiles with `tsc` and runs from emitted
JavaScript (`node dist/...`); it does not rely on Node's direct `.ts` execution.

### Phase 1 — parallel foundations

**Python target foundation**

- Add target/report types, outer orchestrator, Python adapter, and config-v2 parser.
- Refactor command bodies to return reports before rendering.
- Freeze Python-only version-1 output with snapshots and the full current suite.

**Node package foundation**

- Expand the published npm marker package with protocol validation, worker lifecycle,
  and fake request handlers.
- Add clean `npm pack` and installed-tarball handshake tests.
- Implement Python worker client, startup/request timeouts, cancellation, error mapping,
  bounded stderr, and shutdown.

**Vertical tracer**

- As soon as the fake handshake works, thread one hard-coded NodeNext project and one
  free-function spec through minimal discovery, draft IR, API-mirror rendering, a fake
  candidate, overlay validation, and a recoverable write transaction.
- Keep classes, references, workspaces, repair, and generalized routing out of this
  tracer. Its job is to force the Python adapter, JSONL protocol, compiler worker, and
  artifact boundary to meet before their full abstractions are built.
- After the deterministic tracer is green, run and record one manual `gpt-5.6-sol`
  generation smoke. It is not a PR gate and does not make the TS target user-facing.

The Python and Node foundations share only pinned protocol fixtures and may run in
parallel. The tracer starts when both minimal endpoints exist.

Exit gate: the installed wheel can locate the project-local npm worker and complete a
fake handshake; missing Node/package, version mismatch, malformed output, crash, and
timeout have deterministic exit-2 diagnostics; Python behavior is unchanged. The
free-function tracer succeeds end to end, and an invalid fake candidate leaves no
artifact writes.

### Phase 2 — project graph, routes, discovery, and IR

- Parse production/test configs and reference DAGs; create one incremental Program per
  referenced config with root files while retaining solution-only configs as graph nodes.
- Assign project/package ownership and derive name-preserving artifact routes.
- Implement spec/test/contract discovery and the normative authoring grammar.
- Implement TSDoc parsing, semantic IR, transitive type Merkle graph, dependency/API
  digests, and deterministic API-mirror rendering.
- Implement `jaunt sync` for the supported grammar: model-free mirrors, missing
  facades, unbuilt throwing placeholders, and idempotent owner-project transactions.
- Resolve import/dependency graphs; reject runtime spec edges, route collisions,
  undeclared packages, and context cycles.
- Expose `specs` plus analyzer/project diagnostics. Full freshness/status waits for
  Phase 5.

Exit gate: cosmetic mutations preserve IR; referenced-type mutations stale all public
consumers; recursive types terminate; two specs per directory route uniquely; solution
configs, aliases, and cross-project APIs pass; invalid sources have stable locations.
An unbuilt free-function module becomes editor/typecheck-clean after sync while
`jaunt check` still reports it as unbuilt.

### Phase 3 — candidate composition and sound conformance

- Define reserved internal binding output for Codex.
- Compose final exports/TSDoc/header deterministically.
- Implement overlays, production-project and dependent-project validation, exact
  exports, package provenance, and no-write candidate results.
- Implement free-function, overload, generic, constructor, class-member, accessor,
  field, modifier, and inheritance conformance.
- Extend sync placeholder rendering across every supported class/member shape.
- Reject `any`, suppressions, ambient/declaration tricks, boundary casts, unsupported
  nominal members, and model-authored exports.

Exit gate: the complete positive/negative matrix passes, especially narrowed class
methods/constructors under strict TypeScript; failed candidates leave every artifact
byte-identical; validated returned bytes match their content hash.

### Phase 4 — first supported model-backed build slice

- Add TypeScript prompt templates and language-aware Codex generation request.
- Implement one-project `jaunt build --language ts`, repair retries,
  cost/cache/progress, and the recoverable implementation/API/header/sidecar write
  transaction. Pin the initial artifact formats here; alpha changes bump their format
  identifiers and force an explicit rebuild, while beta freezes compatibility.
- Scaffold a real generated JWT example through Jaunt rather than hand-maintaining the
  preview output.
- Verify normal compile, emit, declaration, and execution with a fake backend in CI;
  reserve live Codex smoke for manual/nightly runs.

Exit gate: a fresh project with a missing implementation builds through an in-memory
overlay, writes only validated output, runs with plain Node after compilation, and has
no runtime Jaunt dependency.

### Phase 5 — freshness, mixed workspaces, status, and check

- Add header/sidecar comparison, structural/prose/fingerprint classification,
  semantic gate, restamp/refreeze, dependency API closure, orphan detection, target
  selection, and mixed report aggregation.
- Validate combined overlays across project references.
- Merge Python and TypeScript reports and exit precedence without changing v1 output.
- Add TS magic-mode `check`, `clean --orphans`, route-proving
  `migrate --config-v2`, and target-aware instructions. TS contract checking waits
  for Phase 7; Python contract checking remains active throughout.

Exit gate: body-only dependency rebuilds do not stale consumers; API/type changes do;
equivalent prose refreezes; meaningful prose rebuilds; a mixed root reports only the
truly stale target; `check` is offline and deterministic.

### Phase 6 — Vitest test and repair loop

- Add test-spec IR and TypeScript test prompt.
- Generate/typecheck example and derived batteries.
- Implement disposable Vitest runner, reporter, fixtures, fast-check, leak guard,
  redacted repair, implicated-module mapping, and test JSON/progress/cost.
- Add mutation workers and strength reporting after the base repair loop is green.

Exit gate: example failures retain detail; every derived leak sentinel remains hidden;
typed fixture/property failures stop before execution; sync/async runaways terminate;
repair succeeds without exposing derived source or values.

### Phase 7 — design and contract lifecycle

- Implement design dry-run/apply, contract adopt/reconcile/check/eject, and whole-module
  magic ejection.
- Add dirty/stale digest guards, prevalidated recoverable multi-file patches, cost,
  JSON, and journals.
- Add deterministic migrations for every alpha layout/protocol/IR change.

Exit gate: design cannot edit outside its declaration; failed reconcile/eject writes
nothing; successful magic eject compiles, emits, packs, tests, and contains no Jaunt
runtime/spec/provenance.

### Phase 8 — operations and ecosystem integration

- Add watcher invalidation, daemon/job/proposal/landing support, guard recognition,
  tree/repo-map TypeScript descriptions, and npm auto-skills.
- Add project-reference and multi-package dogfood workspaces.
- Finish docs, schema, examples, plugin skills/hooks, troubleshooting, and security
  model.

Exit gate: daemon proposals contain only allowlisted machine-owned paths and revalidate
on landing; 100 edit/watch cycles leak no processes, handles, listeners, or unbounded
memory; every quickstart command runs in CI.

Before Phase 8, watch/daemon/jobs requested for a configured TS target fail with a
clear unsupported-phase diagnostic; they never silently ignore the TS target.

### Phase 9 — packaging and staged release

- Build wheel/sdist and npm tarball once; test those exact bytes in clean projects.
- Run supported OS/Node/TypeScript/Vitest/compiler-mode matrices.
- Publish alpha, dogfood, freeze beta protocol/IR, then promote to stable only after
  release gates below pass.

## 17. Required test matrix

### 17.1 Node analyzer unit/golden tests

- Marker alias resolution, exact stub forms, illegal executable declarations,
  `@jauntDesign`, `@jauntPreserve`, test targets, and contract tags.
- Deterministic sync rendering for mirrors, new facades, and self-contained throwing
  placeholders across every supported symbol shape; repeated sync is byte-identical.
- TSDoc cleaning, multiline/custom tags, malformed tags, Unicode, CRLF, and source
  locations.
- Cosmetic IR neutrality: whitespace, comments, semicolons, quote style, import alias,
  and equivalent parenthesization.
- Structural IR changes: kinds, optional/rest, type parameters/constraints, overloads,
  modifiers, heritage, class members, and referenced interface/type changes.
- Recursive aliases/interfaces, cross-file and cross-project type closure, external
  package types, deterministic SCC hashing, and callable-intersection ordering.
- Functions: exact, safe wider input, unsafe narrower input, wrong return, optional,
  rest, overloads, generics, explicit `this`, and async.
- Classes: constructor narrowing, method bivariance regression, static/inherited
  members, generic classes, accessors, mutable/readonly fields, and optional
  members.
- Escape attempts: explicit/inferred `any`, suppressions, double assertions, ambient
  augmentation, declaration merging, extra/missing/star exports, and boundary casts.
- API-mirror corruption caught by the independent original-spec equivalence proof.
- `@jauntPreserve` with an allowed paired-context import and a rejected arbitrary
  runtime import.
- Import graph: runtime spec imports through static/dynamic/require forms, direct and
  transitive context cycles, generated-private imports, undeclared hoisted packages,
  package exports/imports, builtins, type-only and test-only rules.
- Emitting configs that accidentally include a private spec or test-spec fail
  preflight with an exact exclusion fix.
- Overlays: first missing output, combined candidates, downstream reference validation,
  stale epoch, cancellation, exact returned-byte hash, and a non-strict owner whose
  unrelated source remains accepted while candidate boundary checks stay strict.

### 17.2 Python unit/integration tests

- Strict version-1/version-2 config, unknown-key suggestions, target migration, globs,
  empty/ambiguous projects, and schema rendering.
- Protocol framing, concurrency serialization, timeout, cancel, crash/restart,
  malformed response, version mismatch, stderr truncation, path containment, and
  deterministic error rendering.
- Target scheduling, dependency failure propagation, jobs limit, partial-owner
  success, cost budget, cache namespaces, progress, and signal cleanup.
- Python-only CLI/JSON snapshots unchanged; mixed build/test/status/check/specs/clean
  reports and exit precedence.
- Header/sidecar parse, refreeze/restamp, API dependency closure, cache validation,
  orphan safety, and atomic-write recovery.
- `sync` makes no model call, never overwrites a facade or real implementation, and
  preserves `unbuilt` status/check semantics after a green owner-project typecheck.
- Context freshness split: signature changes rebuild, meaningful TSDoc changes take
  the prose gate, and body-only edits revalidate without generation.
- Daemon TS job IDs, supersession, retry, patch allowlist, proposal landing/discard,
  notifications, and target-scoped checks.

### 17.3 Hermetic end-to-end fixtures

Fixtures are copied to temporary directories before mutation:

- One strict NodeNext app; one Bundler/Vite app; one CommonJS-emitting app.
- `.tsx` component spec and generated JSX implementation before stable release.
- Two specs in one directory, nested same basenames, spaces/symlinks/case collisions.
- npm and pnpm workspaces; root-hoisted tooling and leaf-owned runtime dependencies.
- `extends`, aliases, `rootDirs`, composite references, solution-only root, separate
  test project, a config that owns root files while referencing another config, and
  cross-project generated dependency.
- A reference graph resolving a worker/compiler identity different from `tool_owner`
  fails before analysis with deterministic install guidance.
- Build with fake backend, typecheck, test, JS emit, declaration emit, run, pack,
  install in a clean consumer, typecheck consumer, and execute consumer.
- The consumer has no Jaunt runtime dependency; package contains no executable spec
  JS; API mirror/declarations and QuickInfo retain authored types and TSDoc.
- Missing output, stale output, invalid candidate, context cycle, runtime spec import,
  orphan, and undeclared package all fail at the intended phase.
- A new spec runs `jaunt sync` with Codex unavailable, typechecks through its facade
  and context, throws if its placeholder is executed, and remains `unbuilt` in check.
- Design dry-run/apply/stale-source/dirty-tree/invalid patch; contract lifecycle; full
  magic eject.

### 17.4 Held-out adversarial tests

Place sentinel secrets in assertion messages, stacks, diffs, snapshots, captured
stdout/stderr, warnings, setup/teardown failures, aggregate causes, config errors, and
worker crashes. None may occur in implementer repair prompts or default JSON. Empty or
corrupt reports produce a minimal redacted fallback, never raw Vitest output.

## 18. Packaging, CI, and publication

### 18.1 npm package

- Publish from the owned `@usejaunt/ts` coordinate. The marker-only alpha bootstrap
  is already tracked; Phase 1 adds the worker without renaming the package.
- The worker entry is pure ESM. The inert marker surface has conditional ESM/CJS
  exports with one shared declaration contract so NodeNext CommonJS specs do not hit
  ESM-import errors. The package has no native addon, install script, network access,
  or lifecycle hook.
- TypeScript is a required direct dev dependency of the configured `tool_owner` and a
  peer in the tested range. Vitest and fast-check are optional package peers but must
  be direct dev dependencies of each owning test package when the corresponding
  feature is used; missing peers produce exact install guidance.
- Python resolves the worker and compiler from the configured `tool_owner`, never a
  compilation/dependency owner or global executable. `JAUNT_TS_WORKER` is a documented
  test/development override only.
- `npm pack --json` allowlists compiled JS, declarations, README, license, and package
  metadata. It excludes fixtures, credentials, raw specs, and absolute source-map
  paths.
- Deterministic `.api.ts` sources contain declarations only. Normal emit may produce
  an empty `.api.js` plus the required `.api.d.ts`; packed-library tests require the
  declaration and permit/prune the empty runtime file according to the package's
  build policy.

### 18.2 Python distribution

- Add target/protocol modules, schemas, TS prompts, and npm-install guidance to wheel
  and sdist.
- The wheel does not bundle a second TypeScript compiler or silently download npm
  packages.
- Clean-wheel tests install only published-style artifacts, then resolve and handshake
  with the local npm package.

### 18.3 Required PR checks

- Existing `uv run pytest`, Ruff lint/format, ty, and `uv run jaunt check`.
- npm clean install, lint, format, typecheck, unit tests, build, and tarball inspection.
- Shared protocol golden fixtures in Python and TypeScript.
- End-to-end compile/emit/declaration/packed-consumer and Vitest lanes.
- Documentation typecheck/build and executable quickstarts.
- Deterministic fake backend for PRs. Live Codex smoke is manual/nightly and always
  uses the repository model policy.

CI runs representative Linux lanes on every PR and path/lifecycle smoke on Windows
and macOS. The full Node/TypeScript/Vitest/OS cross-product may run nightly, but the
minimum and maximum supported versions must both block release.

## 19. Reliability, security, and performance

### Reliability

- Worker stdout is protocol-only; unexpected text is a protocol failure.
- Read-only idempotent analysis may restart/replay once after a crash. Model calls,
  candidate commits, design apply, and lifecycle writes are never blindly replayed.
- SIGINT/SIGTERM cancels model calls, worker requests, Vitest, and mutation process
  groups, then preserves the previous artifact set.
- Every multi-file write has a planned manifest, validated content hashes, sibling
  temp files, and recovery that treats a partial transaction as stale/invalid.
- Status/check cannot depend on ephemeral cache presence. Deleting `.jaunt/` changes
  performance, not answers.

### Security boundary

- Discovery fixtures deliberately contain import-time filesystem, network, child
  process, and exit side effects; analysis must execute none of them.
- The analyzer necessarily executes the installed worker and TypeScript compiler; the
  guarantee is that it never evaluates application/spec/config modules. Python
  scrubs `NODE_OPTIONS`, `NODE_PATH`, custom loader/require flags, and ts-node/tsx
  injection variables before spawn, then supplies an explicit minimal environment.
- All subprocess calls use argument arrays, never shell interpolation.
- Resolve and containment-check every configured, generated, temporary, and deletion
  path after symlink resolution.
- Analyzer protocol rejects paths outside the workspace/package resolution roots and
  caps message/source sizes.
- Generated code and project tests are trusted-project execution, not a sandbox. The
  docs state that distinction plainly.
- Dependency audit distinguishes runtime/type/test imports and checks package
  ownership; successful hoisted resolution is not authorization.
- Release blocks on known high-severity production dependency findings unless a
  time-bounded exception is documented.

### Performance

Benchmark cold worker handshake/discovery, a 1,000-file project graph, one-file warm
analysis, combined overlay validation, Vitest startup, and 100 incremental edits.
Record p50/p95 duration, peak RSS, worker restarts, file descriptors, and child process
count as JSON artifacts.

Set hard budgets from the first alpha baseline. A greater-than-20% regression blocks
only on a dedicated pinned runner; hosted PR runners collect comparison data and
release gates evaluate it. Replace qualitative leak claims with numeric RSS slope,
descriptor/listener deltas, and surviving-process thresholds measured over the
100-edit watch loop.

## 20. Documentation and developer experience

Before beta, add:

- TypeScript overview and quickstart.
- Module layout, authoring grammar, context/downstream layering, and facade/API-mirror
  rationale.
- Tests, held-out behavior, fixtures, properties, and contract mode.
- Project references, package ownership, aliases, monorepos, and dependency errors.
- Application development, emitted libraries, declaration publishing, and eject.
- Command/JSON/exit-code reference and config schema.
- Limitations, unsupported syntax, security boundary, troubleshooting, and upgrade/
  rollback guides.
- Target-aware README, `AGENTS.md`, `CLAUDE.md`, `jaunt instructions`, Codex/Claude
  plugin skills, hooks, and navigation.

Promote the preview to `examples/typescript-jwt` only after it is generated by Jaunt.
Its generated files become read-only artifacts and every documented command runs in
CI. Add a second example for project references rather than turning the JWT example
into a monorepo.

Claims follow fixtures. Do not advertise `.mts`/`.cts`, JavaScript specs, a framework,
runtime, package manager, or version range until its clean fixture is a release gate.

## 21. Release stages and rollback

Python and npm versions are independent; the protocol handshake defines compatible
ranges.

### Internal alpha

- Experimental target behind config version 2 and an npm prerelease dist-tag.
- Dogfood one emitted library and one project-reference monorepo.
- Protocol/IR may change with an explicit rebuild diagnostic; deterministic migrations
  become mandatory once a format reaches public beta.

### Public beta

- Package consumer, cross-platform lifecycle, held-out leak, eject, docs, security,
  and benchmark gates pass.
- Protocol and contract-IR scheme freeze for the beta line.
- Every alpha artifact layout has a migration or an explicit rebuild diagnostic.

### Stable

- At least one complete beta cycle without a protocol/IR reset.
- All advertised matrices pass using the exact wheel/sdist/npm tarball candidates.
- Clean-room install can init, build, check, test, emit, pack, consume, and eject.
- Generated application code remains runnable after uninstalling Jaunt tooling.

Before the first coordinated release, replace the current `release.yml` behavior that
publishes/tags PyPI on any `pyproject.toml` change. Build the wheel/sdist and npm
tarball once, retain checksums, and test those exact artifacts. Publish npm first under
a non-`latest` candidate dist-tag with provenance, publish PyPI through trusted
publishing, install both registry artifacts into clean projects, run the full registry
smoke, and only then move npm `latest` and create releases. Keep existing Python tags
as `vX.Y.Z`; use distinct npm tags such as `ts-vX.Y.Z`.

Rollback uses npm dist-tags/deprecation and PyPI yank where warranted. A patch release
must remain protocol-compatible where possible. Uninstalling tooling does not break
already-generated programs, but faulty generated code still requires reverting or
regenerating the affected project artifacts.

## 22. Global acceptance criteria

The TypeScript target is stable only when all are true:

- No spec/test-spec module executes during build, status, specs, check discovery, or
  contract analysis.
- No required workflow uses a resolver hook.
- Several specs in one directory map without collisions.
- Context cannot reach its own facade/generated layer through a runtime cycle.
- Public generated types and TSDoc match the deterministic API mirror; QuickInfo is
  non-empty.
- Narrowed class constructors/methods, unsafe overload/generic implementations,
  `any`, suppressions, boundary casts, extra exports, and undeclared dependencies fail
  before writes.
- A referenced public type change restales every affected implementation/dependent;
  formatting does not.
- First build validates missing artifacts in memory; validation/model/test failure
  preserves prior artifacts byte-for-byte. A process/power failure may leave a
  manifest-marked partial transaction that the next invocation detects and repairs.
- Build/test/status/check work across project references and mixed Python/TS roots.
- Derived repair feedback passes the adversarial leak suite.
- Normal JS and declaration emit, packed-library consumption, and full eject work
  without publishing raw spec JS or requiring Jaunt at runtime.
- Version-1 Python projects retain current behavior and JSON.
- Full Python and Node suites, lint/format/type checks, Jaunt check, docs build,
  packaged-artifact smoke, and supported compatibility lanes are green.

## 23. Execution rules for implementation PRs

- One phase may span several PRs, but each PR has one primary interface and its tests.
- Pin versioned protocol/IR draft fixtures before parallel Python and TypeScript
  implementations; freeze compatibility only at the public-beta gate.
- Do not mix Python behavior refactors with TS semantics unless the adapter boundary
  requires it; land the Python compatibility wrapper first.
- Never hand-edit existing Python self-hosted `__generated__`, `.pyi`, or contract
  batteries. Change their canonical source and regenerate through Jaunt.
- Once the real TS builder exists, never hand-edit generated TypeScript/API mirrors or
  batteries; fixtures that need invalid artifacts create them in temporary test dirs.
- Unit tasks run focused tests. Integration phases run both complete language suites
  and packaged-artifact checks.
- Every implementation phase ends with a read-only review against `DESIGN.md`, this
  plan, protocol schema, and the phase's acceptance gate.
