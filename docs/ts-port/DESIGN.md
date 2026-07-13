# Jaunt-for-TypeScript — design

Status: **validated architectural spike** — the direction survived one
external design review (which reshaped substitution and designed APIs; see
the decision log), and every choice below is exercised by the runnable
preview in [`example/`](example/README.md). Not yet an implementation plan.

"Porting Jaunt to TypeScript" means building **Jaunt-for-TypeScript** — TS
specs in, TS implementations and vitest tests out — not translating 31k
lines. Jaunt operates *on* its host language: it parses specs, digests ASTs,
emits code, and judges it with the language's test runner. The codebase
splits into four buckets:

| Layer | ~Share | Verdict |
|---|---|---|
| Orchestration: builder scheduler, codex backend, daemon, CLI, config, watcher | ~45% | near-mechanical 1-1 port (or reused outright — see "implementation strategy") |
| Static analysis: digests, dep inference, validation, class analysis, stub emission | ~30% | concept-for-concept, re-derived against TS grammar |
| Runtime registration: `runtime.py`, `module_magic.py`, import machinery, registry | ~10% | **do not port** — delete and redesign smaller |
| Test judge: tester, held-out plugin, contract runner, strength, properties | ~15% | equivalents exist; every piece is "80% there, different shape" |

## Governing principle: static-first

Python Jaunt executes user modules because that is the only way to get real
signatures, inheritance, and resolved references — hence frame inspection,
two-phase `magic_module` registration, `sys.modules` eviction, live module
rebinding, and the registry split-brain hazards. The TS compiler answers all
of those questions from source. So the rule for the port:

> Every question Jaunt asks must be answerable by parse + typecheck alone.
> User code never runs during discovery — and spec files never run at all.

Discovery becomes pure scan; there is no registry, no import-order hazard,
and the "long-lived process can't see spec edits" limitation disappears.

## Module layout and substitution: the generated facade

ESM namespaces are sealed — no `__getattribute__` traps, no rebinding, no
reload — and the review established that resolve-time redirection cannot be
the correctness path either: it cannot rewrite intra-module lexical bindings
(a handwritten helper calling a sibling stub is unfixable by any hook), it
dies under tsc emission (`dist/*.js` paths never hit a source manifest), and
it rides on release-candidate loader APIs. Substitution is therefore a
**build-time layout**, not a runtime mechanism:

```
src/tokens/
  spec.jaunt.ts        authored contracts + stubs; never imported at runtime
  context.ts           handwritten executable context (ordinary module)
  index.ts             ordinary public facade — what consumers import
  __generated__/
    impl.ts            generated implementation, authored-type-annotated
```

- **`spec.jaunt.ts` is a build input, not a runtime module.** Types flow out
  of it only through erased positions (`import type`,
  `typeof import("./spec.jaunt.ts")`), so consumers carry no jaunt runtime
  dependency and stubs never evaluate.
- **Executable handwritten context lives in `context.ts`**, an ordinary
  module the generated impl imports and the facade re-exports. Spec files
  hold stubs plus *type* context (interfaces, type aliases) only. This
  eliminates the lexical-binding trap by construction — code that consumes
  governed functions imports the facade like any other consumer. The
  scanner enforces it: executable declarations in a `*.jaunt.ts` file are
  an error.
- **`index.ts` is boring committed code**: `export * from "./context.ts";
  export * from "./__generated__/impl.ts"; export type { ... } from
  "./spec.jaunt.ts";`. One module graph; ordinary tsc, bundler, test, and
  publish behavior; Bun/Deno/webpack/Next need nothing.
- **A missing build is an honest compile failure** — the facade's import of
  `__generated__/impl.ts` doesn't resolve — surfaced by `tsc`/`jaunt check`,
  not a runtime mystery. (`jaunt init` may scaffold a throwing impl for
  gentler onboarding; that's sugar, not architecture.)
- The old resolve-hook design survives only as **optional dev convenience**
  (`example/src/jaunt/register.mjs`): a scratch script importing a spec path
  gets redirected to the sibling facade. Nothing correctness-critical may
  depend on it. Package `exports` conditions remain available polish at
  package boundaries.

## Conformance: authored-type-annotated exports

Enforcement is assignability, not text — but not via a separate check file.
The generated module's own exports carry the authored type:

```ts
const createTokenImpl = /* generated implementation */;
export const createToken: typeof import("../spec.jaunt.ts").createToken =
  createTokenImpl;
```

One mechanism, two guarantees, verified in the preview both positively and
negatively (drifting a generated parameter type fails `tsc` on exactly the
annotated line):

1. **Enforcement** — a generated signature that isn't assignable to the
   authored one is a compile error inside jaunt's validation pass.
2. **Pinning** — consumers see exactly the authored contract type, never an
   accidentally-wider generated type.

For classes, the value export is annotated with the authored constructor
type and the instance type is re-exported from the spec
(`export type TokenStore = import("../spec.jaunt.ts").TokenStore`).

Consequences, unchanged from the pre-review design: `@jaunt.sig` does not
exist (conformance is the default); Liskov-shaped widening inside the impl
is fine; the guidepost's *useful* freedom relocates to private members and
internal helpers, invisible to the declared type; freedom in a public
signature is declared as looseness in the type itself. Param names and
default values get a cheap advisory AST lint. `@jaunt.preserve` as a
*decorator* is gone too — native Node type-stripping rejects decorator
syntax — so the rare "real method whose body looks like a stub" corner is
marked with a `@jauntPreserve` TSDoc tag instead.

Open proof obligations (from the review, deliberately not hand-waved):
overloads, generics, and abstract classes need a conformance test matrix —
`typeof` annotation covers the common cases, but the matrix decides where
per-overload assertions or instance-side checks are additionally required.

## Designed APIs: `jaunt design`

Docstring-only "the model designs the API" specs are **not** part of the
runtime design. The review killed the barrel-typed variant for good reasons
(consumers importing the spec path see a wrong/empty type; without tooling
a designed class constructs silently instead of throwing; "which exports
need the barrel" is tribal knowledge). Replacement:

> `jaunt design` — the model proposes a *declaration patch* to the spec
> file (signatures + TSDoc) for human review. On acceptance, the symbols
> are ordinary declared APIs and normal generation proceeds.

The contract stays reviewable text in the spec; "types never lie" holds
uniformly; and there is Python precedent for tool-writes-spec-behind-review
(`jaunt adopt`, `jaunt migrate --apply`). The preview shows the end state:
`TokenStore`'s accepted declaration, conformance-checked like everything
else.

## Contract convention: TSDoc

The contract is the TSDoc block immediately preceding the governed
declaration. The TS parser already attaches JSDoc AST nodes to declarations
(`ts.getJSDocCommentsAndTags`) — no trivia archaeology. Jaunt defines its
own `clean()` (strip `/** */` and leading `*`, dedent, trim) and versions it
in the digest scheme.

- **Prose vs structure**: structural digest from the declaration shape;
  prose digest from cleaned TSDoc text. `@param`/`@returns` *text* counts as
  prose (semantic-gate judged); the types are structure.
- **Sections become standard tags**: `Errors:` ⇒ `@throws`, `Examples:` ⇒
  `@example` (both standard JSDoc, IDE-rendered), `Fixtures: db` ⇒
  `@fixtures db`, `Properties:` bullets ⇒
  `@prop given <name>: <fc-arbitrary> :: <invariant>`. The `fc.*` strategy
  text is verified when the battery is rendered and typechecked (not
  before — it is comment text until then).
- **Digest inputs are jaunt-owned IR, not printer output.** The review is
  right that `ts.createPrinter` is not a semantic canonicalizer (it
  preserves original text for positioned nodes). Digests come from a
  **versioned semantic JSON representation** — name, parameter list
  (name/type-text/optionality/default-presence), return type, modifiers,
  member records, prose — extracted from the AST under jaunt's own
  normalization rules, exactly like Python's `normalized_contract`
  field-splitting, with the scheme version carried in headers. New
  ecosystem ⇒ no legacy-digest compatibility layer.

## Test judge: Vitest

- **Single runner** (programmatic API for the repair loop, custom reporters,
  `test.extend` fixtures). Python's hybrid in-process-eval/pytest paths in
  `contract/runner.py` collapse.
- **Held-out tiers by filename** (`.example.test.ts` / `.derived.test.ts`) —
  jaunt writes these files, so no marker plumbing. A ~100-line custom
  reporter replaces the 391-line `heldout.py` pytest plugin: same JSON
  report, same derived-tier redaction, same leak assertion.
- **Fixtures**: `tests/fixtures.ts` exporting `base.extend({...})` is the
  conftest analog; `@fixtures name` resolves to a property on a typed
  object, so a missing fixture is a *compile* error.
- **Properties**: fast-check. Seed derived from the case digest + fixed
  `numRuns` replaces `derandomize=True + database=None` (fast-check persists
  nothing — no shrink-cache redirection). `fc.asyncProperty` lifts the
  Python v1 async restriction.
- **Mutation strength**: same mechanical AST mutants; execution moves to a
  `worker_threads` pool with `worker.terminate()` on timeout — replaces
  POSIX `SIGALRM`, kills sync *and* async runaways, gives fresh module state
  per mutant, and parallelizes.

## Config: `jaunt.toml` stays canonical

Code-as-config (`jaunt.config.ts`) is the TS idiom, but wrong for this tool:
config values feed build digests (dynamic config churns fingerprints), and
executing user config violates static-first. TOML is not alien in JS tooling
(bunfig, netlify); editor ergonomics come from a published JSON Schema via
taplo/Even Better TOML. The decisive property: **one root `jaunt.toml`
governs mixed repos** — shared `[codex]`/`[semantic_gate]`/`[daemon]`
sections plus `[target.py]` / `[target.ts]` (the TS target adds
`spec_suffix = ".jaunt.ts"`). Workspace routing needs *both* manifests:
nearest `package.json` for dependency ownership, `tsconfig.json`/project
references for compilation ownership. Rejected: a `"jaunt"` field in
package.json (second config source, fragments digests).

## Implementation strategy (open, review-recommended)

The reviewer proposes retaining the **existing Python orchestration core**
(builder scheduler, daemon, CLI, config, journal) and adding a **Node
worker over a versioned JSON protocol** for the TS-specific parts:
`ts.Program` services, contract-IR extraction, conformance checking, and
Vitest runs. That reuses the ~45% bucket outright instead of transcribing
it, fits the mixed-repo direction (one daemon, one polyglot `jaunt check`),
and confines TS code to the analysis/judge surface. The alternative is a
full TS rewrite of the orchestration spine. Decision deferred to
implementation planning; the worker approach is the current default.

## Decision log

1. **Codex generates `.ts`** (not `.js`+`.d.ts`): one language, one
   `ts.Program` for all validation, eject stays maintainable, Node 22.18+
   type-stripping runs it directly.
2. **Idiomatic over Python-parity** wherever they conflict.
3. **Support floor: Node ≥ 22.18**, Vite/Vitest, esbuild.
4. **`jaunt.toml` stays**; no `jaunt.config.ts`, no package.json field.
5. **Tag naming**: prefixed markers (`@jauntContract`, `@jauntPreserve`),
   unprefixed sections (`@fixtures`, `@prop`).
6. *(review round)* **Substitution = generated facade**, spec files never
   imported at runtime, executable context split into `context.ts`;
   resolve hooks demoted to optional dev sugar.
7. *(review round)* **Conformance = authored-type-annotated exports** in
   the generated module (enforce + pin), replacing the separate
   `satisfies` check file.
8. *(review round)* **Designed APIs = `jaunt design`** declaration-patch
   flow; barrel-typed designed APIs dropped.

## Next steps

1. Specify the versioned contract-IR (the digest input) — every other
   subsystem hangs off it, and it doubles as the Node-worker protocol's
   core payload.
2. Build the conformance test matrix: overloads, generics, abstract
   classes, getter/setter pairs, negative cases.
3. Decide the implementation strategy (Python core + Node worker vs full
   TS rewrite) and version the worker protocol.
4. Prototype `jaunt design`'s patch-and-review UX on the example.
