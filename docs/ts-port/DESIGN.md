# Jaunt-for-TypeScript — design

Status: agreed direction, pre-implementation. A runnable preview of every
choice below lives in [`example/`](example/README.md).

"Porting Jaunt to TypeScript" means building **Jaunt-for-TypeScript** — TS
specs in, TS implementations and vitest tests out — not translating 31k
lines. Jaunt operates *on* its host language: it parses specs, digests ASTs,
emits code, and judges it with the language's test runner. The codebase
splits into four buckets:

| Layer | ~Share | Verdict |
|---|---|---|
| Orchestration: builder scheduler, codex backend, daemon, CLI, config, watcher | ~45% | near-mechanical 1-1 port |
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
> User code never runs during discovery.

Discovery becomes pure scan; there is no registry, no import-order hazard,
and the "long-lived process can't see spec edits" limitation disappears.

## Authoring surface

TC39 decorators cannot decorate plain functions, so the file-pragma style —
already Jaunt's primary style — is the only survivor, and the stub form
carries everything else:

- **Governance**: `jaunt.magicModule()` at the top of the file; detected
  statically (alias-resolved), runtime no-op. Module kwargs merge key-by-key
  into every governed spec, as in Python.
- **Stub form**: a body that is exactly `return jaunt.magic(opts?)` (or a
  bare `jaunt.magic()` statement in a `void` function). `magic(): never` is
  assignable to every return type, so one form typechecks everywhere; at
  runtime it throws `JauntNotBuiltError` — Python's placeholder-class
  semantics for free.
- **Per-symbol config** rides inside the `magic()` call. `deps` are real
  imported identifiers: checker-resolved, refactor-safe, and exact — a
  strict upgrade over both Python's string refs and its heuristic AST
  inference (`deps.py`'s name-collector becomes `getSymbolAtLocation`).
- **Classification** mirrors Python module-magic: real body ⇒ handwritten
  context (read, never regenerated); interfaces/type aliases ⇒ always
  context; docstring-only class (empty body + TSDoc) ⇒ designed API;
  non-jaunt decorator ⇒ ungoverned. "Last binding wins" disappears
  (duplicate top-level declarations are a compile error). The one surviving
  sharp edge: same-file module-scope calls to a governed stub see the stub
  (ESM local bindings can't be redirected) — keep the static warning.
- **Test specs**: stub functions whose body is `jaunt.testSpec({ targets })`
  in files under `test_roots`, named so vitest never collects them.
- **Contract mode**: marks *real* code, so no wrapper — a `@jauntContract`
  TSDoc tag written by `jaunt adopt`.
- **`@jaunt.preserve`** survives (TS 5 decorators work on class members) but
  only for its corner case: a real method whose body looks like a stub.

## Contract convention: TSDoc

The contract is the TSDoc block immediately preceding the governed
declaration. The TS parser already attaches JSDoc AST nodes to declarations
(`ts.getJSDocCommentsAndTags`) — no trivia archaeology. Jaunt defines its
own `clean()` (strip `/** */` and leading `*`, dedent, trim) and versions it
in the digest scheme.

- **Prose vs structure**: structural digest = canonical print of the
  declaration (`ts.createPrinter({ removeComments: true })`), stub bodies
  collapsed, jaunt markers stripped; prose digest = cleaned TSDoc text.
  `@param`/`@returns` *text* counts as prose (semantic-gate judged); the
  types are structure. Byte-stability comes from pinning the `typescript`
  version and embedding a `ts5.x` tag in the digest scheme — the same trick
  as the parse cache's `py313` tag, minus the pickle. New ecosystem ⇒ no
  legacy-digest compatibility layer; design the normalization once.
- **Sections become standard tags**: `Errors:` ⇒ `@throws`, `Examples:` ⇒
  `@example` (both standard JSDoc, IDE-rendered), `Fixtures: db` ⇒
  `@fixtures db`, `Properties:` bullets ⇒
  `@prop given <name>: <fc-arbitrary> :: <invariant>` with strategies as
  real `fc.*` expressions (checker-verified).

## Conformance: "types never lie" replaces the three tiers

Python needed sealed/guidepost/preserved because nothing stood between the
declared signature and the consumer. TS has a compiler, so:

- **Enforcement is assignability, not text.** Per declared symbol, the build
  emits one line into `__generated__/*.check.ts`:
  `gen.f satisfies typeof spec.f;` — verified by the in-process `ts.Program`
  validation pass (which also replaces the `ty` subprocess and the
  undeclared-import provenance walk; package.json dependencies are right
  there). Liskov-shaped widening is allowed; exact-text matching is not the
  TS notion of "same signature". Param names and defaults get a cheap
  advisory AST lint.
- **`@jaunt.sig` is deleted** — conformance is the default, and it composes
  with generics and overloads, which the canonical-signature JSON could not
  represent.
- **Guidepost freedom relocates to where types don't reach**: private
  members, internal helpers, splitting logic — invisible to the declared
  public type, hence unconstrained. Freedom in a *public* signature is
  declared as looseness in the type itself (`opts?: Record<string,
  unknown>`), which is how TS authors already communicate intent.
- **Designed APIs (docstring-only specs)**: Python's `.pyi`-shadows-`.py`
  trick has no TS equivalent (a sibling `.d.ts` cannot override a `.ts`, and
  module augmentation can't add constructors/statics). The idiomatic answer
  is the **barrel**: designed symbols re-export from `__generated__`, so
  both types and runtime flow from the model's design; declared symbols
  re-export from the spec. One-sentence rule: *if you declared it, the
  compiler holds the model to it; if you asked the model to design it,
  import it through the barrel.*

## Resolution: redirect at resolve time

ESM namespaces are sealed — no `__getattribute__` traps, no rebinding, no
reload. Substitution moves from attribute-rebinding time to resolve time:

- `jaunt build` writes `src/<pkg>/__generated__/<mod>.ts` (generated output
  **is TypeScript**), which imports handwritten context from the spec module
  and re-exports it, mirroring Python's generated modules.
- A resolution hook redirects imports of a governed spec module to its
  generated sibling, **except when the importer is that spec's own generated
  module** (the raw-spec exception replaces `__jaunt_original_stubs__`).
- Three thin adapters over one core rule: `jaunt/register` (Node
  `module.registerHooks`, ≥22.15 — synchronous, same-thread), `@jaunt/vite`
  (covers Vite and Vitest), and an esbuild plugin. The redirect map is
  derivable from `jaunt.toml` + the pragma scan; `.jaunt/ts-manifest.json`
  caches it for the runtime resolvers.
- Degradation: no build ⇒ fall through to the spec, stubs throw
  `JauntNotBuiltError` naming both fixes. No resolver ⇒ same error. `tsc` is
  completely resolver-unaware — types come from spec declarations.

## Test judge: Vitest

- **Single runner** (programmatic API for the repair loop, custom reporters,
  `test.extend` fixtures). Python's hybrid in-process-eval/pytest paths in
  `contract/runner.py` collapse.
- **Held-out tiers by filename** (`.example.test.ts` / `.derived.test.ts`) —
  jaunt writes these files, so no marker plumbing. A ~100-line custom
  reporter replaces the 391-line `heldout.py` pytest plugin: same JSON
  report, same derived-tier redaction, same leak assertion.
- **Fixtures**: `tests/fixtures.ts` exporting `base.extend({...})` is the
  conftest analog; `@fixtures name` resolves to a property on a typed object,
  so a missing fixture is a *compile* error, earlier than pytest can catch it.
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
sections plus `[target.py]` / `[target.ts]`, with workspace routing unified
as "nearest owning manifest" (`pyproject.toml` ↔ `package.json`). One
daemon, one polyglot `jaunt check` gate. Rejected: a `"jaunt"` field in
package.json (second config source, fragments digests).

## Decision log

1. **Codex generates `.ts`** (not `.js`+`.d.ts`): one language, one
   `ts.Program` for all validation, eject stays maintainable, and Node
   22.18+ type-stripping runs it directly.
2. **Idiomatic over Python-parity** wherever they conflict — the concrete
   consequence is the conformance design above (no sealed tier, no
   warn-only drift; exported signature drift is a type error).
3. **Support floor: Node ≥ 22.15** (`registerHooks`), Vite/Vitest, esbuild.
4. **`jaunt.toml` stays**; no `jaunt.config.ts`, no package.json field.
5. **Tag naming**: prefixed marker (`@jauntContract`), unprefixed sections
   (`@fixtures`, `@prop`).

## Next steps

1. Prototype the resolution adapter against a real Vite app (HMR interplay
   is the main unknown the preview doesn't cover).
2. Write the digest-normalization spec (canonical print rules, TSDoc clean
   semantics, structure/prose boundary) — every other subsystem hangs off it.
3. Then the port order follows the buckets: orchestration spine mostly
   transcribes; the AST layer re-derives against `ts.*`; the judge builds on
   the reporter + fixtures + fast-check shapes proven in the example.
