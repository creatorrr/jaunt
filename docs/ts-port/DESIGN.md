# Jaunt-for-TypeScript — design

Status: **reviewed architectural spike** — the direction survived two external
design reviews. The runnable preview in [`example/`](example/README.md) proves
the first-review facade and test feasibility; it does not yet exercise the
second-review API mirror, synthetic class adapters, or project graph. Updating
and rerunning that proof is the first implementation gate.

Execution is specified in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md),
including the second-review corrections for class variance, context layering,
transitive type freshness, project graphs, and publication artifacts.

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

```text
src/tokens/
  index.jaunt.ts       authored contracts + stubs; never emitted or executed
  index.context.ts     handwritten leaf dependencies (optional)
  index.ts             ordinary committed public facade
  __generated__/
    index.api.ts       deterministic declaration mirror; no runtime behavior
    index.ts           generated implementation
```

- **`index.jaunt.ts` is a private analysis input.** It is excluded from every
  emitting program. Production code does not import it even in a type
  position, so declaration output and packed libraries cannot leak a private
  source path.
- **Jaunt renders `index.api.ts` deterministically from contract IR.** The
  facade, context, and generated implementation type against this mirror.
  The analyzer separately proves that the mirror matches the original spec
  Program and that the raw implementation matches the original authored
  symbols; a renderer bug therefore cannot make both sides agree incorrectly.
- **Executable handwritten context is a strict leaf.** The generated module
  may import `index.context.ts`, but context may not value-import its own
  facade, generated implementation, or a spec. Ordinary downstream code can
  import the facade; the facade does not re-export that downstream consumer.
- **`index.ts` is boring committed code** with normal exports from the API
  mirror, optional context, and generated implementation. Source specifiers
  follow the owning project's runtime convention (normally `.js` under
  Node-style resolution), so ordinary tsc, bundler, test, and publish flows
  need no Jaunt loader.
- **An unbuilt module stays visibly unbuilt.** Without synchronization, its
  missing implementation is a normal compiler error. `jaunt sync` may write a
  deterministic API mirror and typed throwing placeholder to restore editor and
  project typing without a model call; provenance keeps `status`/`check` in the
  unbuilt state until a validated implementation replaces it. First-build
  analysis can create the same placeholder in memory when sync was skipped.
- Resolve hooks may survive only as **optional development convenience**.
  Nothing in build, check, test, emit, publish, or consumer execution may
  depend on one.

## Conformance: deterministic boundaries plus semantic checks

Codex writes reserved internal bindings, not the public export boundary:

```ts
const __jaunt_impl_createToken = /* generated implementation */;
```

Jaunt parses those bindings, rejects model-authored exports, and appends the
TSDoc-bearing exports typed from `index.api.ts`. This pins what consumers see
without giving the model a boundary at which to hide an assertion or
suppression. Exact exports, overloads, generic constraints, modifiers,
accessors, package provenance, and `any`/suppression/cast escape hatches are
checked separately.

Whole-class assignability is insufficient because TypeScript methods and
constructors are bivariant. Jaunt therefore generates a non-emitted adapter
for every authored constructor, method overload, accessor side, and generic
environment. Each adapter accepts authored parameters and calls the raw
implementation; the call checks inputs contravariantly and its annotated
return checks outputs covariantly. Unsupported shapes fail discovery rather
than falling back to unsound class assignment.

`@jaunt.sig` does not exist: conformance is the default. A real method that
must be copied into generated output uses the `@jauntPreserve` TSDoc tag
because native Node type stripping rejects decorator syntax. Governed
parameter initializers are rejected in v1; authors express default behavior
with an optional parameter plus TSDoc. Public abstract classes and authored
nominal private/protected members are also deferred until their identity and
construction semantics have a proven conformance model.

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
  **versioned semantic JSON representation** — declaration kind, parameters,
  recursively normalized types, overloads, modifiers, members, referenced
  symbol IDs, and prose — extracted under Jaunt's own normalization rules.
  A Merkle graph carries referenced public types transitively, including
  recursive SCCs and project references. Printer output and
  `checker.typeToString()` are never digest inputs; the scheme version is
  carried in headers and sidecars.

## Test judge: Vitest

- **Single runner**: a disposable subprocess invokes Vitest's programmatic API
  with explicit files and protected settings. Python's hybrid
  in-process-eval/pytest paths in `contract/runner.py` collapse.
- **Held-out tiers by filename and provenance** (`.example.test.ts` /
  `.derived.test.ts`). Repair results are constructed from an allowlist of an
  opaque case ID and normalized exception category; child output is captured,
  user reporters are disabled, and a leak assertion is a second defense.
- **Fixtures**: `tests/fixtures.ts` exporting `base.extend({...})` is the
  conftest analog; `@fixtures name` resolves to a property on a typed
  object, so a missing fixture is a *compile* error.
- **Properties**: fast-check. Seed derived from the case digest + fixed
  `numRuns` replaces `derandomize=True + database=None` (fast-check persists
  nothing — no shrink-cache redirection). `fc.asyncProperty` lifts the
  Python v1 async restriction.
- **Mutation strength**: same mechanical AST mutants; every mutant runs in a
  disposable subprocess/process group with a hard timeout. A coordinator may
  bound concurrency, but killing a mutant also kills its descendants and
  guarantees fresh module state.

## Config: `jaunt.toml` stays canonical

Code-as-config (`jaunt.config.ts`) is the TS idiom, but wrong for this tool:
config values feed build digests (dynamic config churns fingerprints), and
executing user config violates static-first. TOML is not alien in JS tooling
(bunfig, netlify); editor ergonomics come from a published JSON Schema via
taplo/Even Better TOML. The decisive property: **one root `jaunt.toml`
governs mixed repos** — shared `[codex]`/`[semantic_gate]`/`[daemon]`
sections plus `[target.py]` / `[target.ts]` (the TS target adds
explicit production/test `projects`, a `tool_owner`, source/test roots, and
per-target generated directories). Workspace routing needs *both* manifests:
the containing package for dependency ownership and configured
`tsconfig.json`/project references for compilation ownership. Every facade,
spec, artifact, and test has one unambiguous project role. Rejected: a
`"jaunt"` field in package.json (second config source, fragments digests).

## Implementation strategy

Retain the **existing Python orchestration core** (Codex integration, CLI,
config, journal, cost, progress, watch, and daemon) and add a **Node
worker over a versioned JSON protocol** for the TS-specific parts:
`ts.Program` services, contract-IR extraction, conformance checking, and
Vitest coordination. Python and TypeScript keep independent internal
schedulers behind target adapters; an outer orchestrator shares the Codex
semaphore and common services without inventing a cross-language dependency
DAG. The analyzer uses the project-local TypeScript compiler and never
executes application modules or user config.

## Decision log

1. **Codex generates `.ts`** (not `.js`+`.d.ts`): one source language, native
   checker validation, ordinary project emit, and maintainable ejection.
2. **Idiomatic over Python-parity** wherever they conflict.
3. **Tool-host and target-runtime compatibility are separate.** Freeze the
   worker's Node/TypeScript range with its package and test target runtimes
   independently.
4. **`jaunt.toml` stays**; no `jaunt.config.ts`, no package.json field.
5. **Tag naming**: prefixed markers (`@jauntContract`, `@jauntPreserve`),
   unprefixed sections (`@fixtures`, `@prop`).
6. *(review round)* **Substitution = name-preserving generated facade**,
   private specs excluded from emit, executable context a strict leaf, and
   resolve hooks demoted to optional development sugar.
7. *(review round, tightened in round two)* **Conformance = deterministic
   API-mirror-typed exports plus semantic checks**, including synthetic class
   adapters rather than whole-class assignability.
8. *(review round)* **Designed APIs = `jaunt design`** declaration-patch
   flow; barrel-typed designed APIs dropped.
9. *(second review)* **Publication types = deterministic API mirror**;
   production and emitted declarations never import the raw spec.
10. *(second review)* **Class conformance = per-member synthetic adapters**;
    whole-class assignment is not accepted as proof.
11. *(second review)* **Context = strict leaf**, with ordinary downstream
    consumers outside the facade/context/generated cycle.
12. *(implementation planning)* **Python core + project-local Node worker**,
    selected projects, versioned JSONL protocol, and target adapters.
13. *(package naming)* **The npm coordinate is `@usejaunt/ts`.** Unscoped
    `jaunt` and the `@jaunt` scope belong to unrelated publishers, while npm's
    similarity guard rejected `jaunt-ts`. The owned `usejaunt` scope is explicit,
    avoids a naming dispute, and leaves space for future packages with distinct
    jobs. `@usejaunt/jaunt` remains unused unless an actual umbrella package is
    designed.
14. *(authoring loop)* **`jaunt sync` is deterministic and model-free.** It
    renders API mirrors plus explicitly unbuilt throwing placeholders, allowing
    the editor and context layer to typecheck before the first paid build.

## Next steps

Execute the phased plan in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).
Phase 0 pins initial protocol/IR drafts, package ownership, the corrected preview
layout, and the positive/negative conformance matrix before the Python and Node
foundations proceed in parallel. Protocol and IR compatibility freeze at beta,
after checker behavior has exercised the drafts.
