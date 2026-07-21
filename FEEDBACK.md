# Adoption feedback

Running notes from real adopters. Newest section first.

## 2026-07-21: a one-line gql string edit restales four unrelated batteries and hard-fails PR CI

Under published Jaunt 1.7.8 / `@usejaunt/ts` 0.1.0-alpha.7, adding one field
name inside a `gql` tagged-template string in a plain app module
(`src/lib/graphql/queries/briefs.ts`) restaled all four dashboard battery
targets at once (`battery_fingerprint, target_api_digest`), so `jaunt check`
exited 4 and blocked two pull requests that never touched a governed module,
a battery, or a target. Bisecting the branch diff file-by-file isolated the
single line; reverting it restored zero diagnostics. The five governed spec
modules also went structurally stale from the same edit.

The mechanism, as far as we can trace it: `collectTypeEnvironment` follows
every static import reachable from a target, and `semanticSource` keeps all
value initializers in the digest — only bodies of callables with explicit
return annotations are masked. Our pure spec modules type-import shared
shapes from app modules (`transport`, `services/mcp/briefs`,
`components/memory/entity-highlights`), so their closures cover most of the
dashboard app graph, and any edit inside that graph — even one that cannot
affect a type — restales the batteries. A tagged-template literal's contents
can never change TypeScript types (the tag receives `TemplateStringsArray`;
literal inference is erased), so this particular dependency is provably
false.

The cost asymmetry is what makes this a CI problem rather than a rebuild
annoyance. The free restamp path in the tester only accepts a
`target_api_digest` mismatch when a prior build proved the API transition,
and that proof lives in local state a PR branch does not have. So the
prescribed remedy for a semantics-neutral string edit is a paid
`jaunt test --language ts` per branch. Practically, every PR that edits any
file in a battery target's import closure now fails `check` until someone
spends model calls on it. Two PRs were blocked the first business day after
the batteries landed; the closure is wide enough that most dashboard work
will keep hitting this.

Requests, in order of value to us:

- Mask tagged-template literal contents in `semanticSource` the same way
  explicitly-annotated function bodies are masked. This is type-safe and
  fixes the trigger we hit.
- Give `check` a free path to verify a suspected-stale battery against the
  target's actual contract (or accept a committed API-transition proof), so
  a digest-level false positive does not force paid regeneration in CI.
- Per-diagnostic drift attribution: when a battery is stale for
  `target_api_digest`, name the closure file whose record changed. We found
  ours only by bisecting a 20-file branch diff one file at a time.

We will mitigate on our side by moving the shared types our pure modules
import into import-free leaf modules to shrink the closures, but the default
geometry — spec modules that type-import from app code — seems common enough
that other adopters will land in the same trap. This is the same family as
the 2026-07-16 `packageManager` finding: content that cannot change the
contract still restales it, and the remedy is paid.

## 2026-07-16: package-manager metadata alone forces paid TypeScript rebuilds

Adding only `"packageManager": "pnpm@11.5.0"` to the dashboard
`package.json`, with dependencies, lockfile, `node_modules`, specs, and compiler
inputs unchanged, made all 19 TypeScript modules structurally stale and all 30
batteries stale under published Jaunt 1.7.8 / `@usejaunt/ts` 0.1.0-alpha.7.
`migrate --language ts --json` offered 19 `model-rebuild` actions and zero free
recompositions. Removing that one field immediately restored 19/19 fresh with
zero diagnostics. We correctly refused the paid rebuilds and pinned pnpm 11.5.0
in CI and Docker outside `package.json` instead.

This independently confirms the package-manager provenance issue. Please treat
`packageManager` metadata as tool/provenance input rather than semantic-contract
input when installed declarations are unchanged, and have per-record status
identify this exact source of drift.

## 2026-07-15: local 1.7.9 / TS 0.1.0 lands 19 frontend specs with excellent cache reuse

The completed migration has 19 Jaunt implementation specs covering 55 exports,
with 15 generated test intents retained. Exact TypeScript and policy
diagnostics reached per-battery retries instead of the old generic fallback.
Six-way generation concurrency and protected candidate validation both worked;
the worker stayed around 836 MiB RSS and protected runners were typically
around 700 MiB, materially below the earlier multi-gigabyte worker peaks.

The first full run made 40 model calls with 15 cache hits and 21 misses. It used
16,812,935 tokens and cost $35.119666. The calls were 21 initial generations
plus 19 diagnostic-driven retries. Accepted candidates survived that failed
run, and later intent edits invalidated only their own requests.

The final protected combined run passed all 30 selected tiers. Twenty-eight
were cache hits and two were already fresh, so the run made zero model calls
and cost $0. The final Jaunt check reported zero unbuilt modules and zero
orphans. Cache preservation and reuse were excellent across repeated failed,
targeted, and final full-workspace runs.

The completed state was then revalidated against clean Jaunt source HEAD
`2b168db`, installed editable and reporting 1.7.9. A full
`test --language ts --no-build --json --progress plain` run found all 30
batteries fresh and still executed the final isolated protected Vitest
validation: 30/30 passed, with zero API calls and $0 cost. A mixed Python and
TypeScript `check` also passed with zero unbuilt modules and zero orphans.

Four implementation modules remain governed by Jaunt, but their unreliable
generated test intents were removed in favor of existing native Vitest
coverage: `brief-draft-proposal`, `select-starter-topics`,
`onboarding-steps`, and `briefs-nav-model-core`. Jaunt currently has neither
per-tier selection on an intent nor a config-level per-intent opt-out, so one
repeatedly bad `example` or `derived` tier cannot be quarantined while keeping
the other generated tier.

Those abandoned batteries repeatedly regenerated forbidden dynamic loaders or
type-invalid fixtures even after receiving exact compiler and policy feedback.
Other candidates passed the compiler gate but then failed public assertions in
the final protected run; those behavioral failures did not enter a useful
candidate repair loop. Exact diagnostics helped, but adopters still had to
repeat fixture shapes, required fields, discriminants, static-import rules, and
safe malformed-input construction across several intents. Native Vitest was
the more reliable boundary for these four modules.

Operational findings and follow-ups:

- A successful targeted implementation build used three model calls, but its
  mixed-workspace JSON omitted `candidate_outcomes`. The updated local source
  now retains those outcomes in mixed payloads; the final cache-only battery
  run did not independently re-exercise a paid implementation build.
- The initial full TypeScript run was silent under `--progress plain --json`.
  Updated local source now constructs and passes the reporter through
  TypeScript-only and mixed build/test paths, including nested implementation
  work. A later targeted retry confirmed live stderr paths, tiers, retry
  diagnostics, and completion counts while stdout remained one JSON object.
- Two earlier targeted retries aborted on transient `gpt-5.6-sol` capacity
  errors without a report. The updated local source adds infrastructure retries
  and structured terminal outcomes; the capacity condition did not recur after
  that patch, so this recovery path was not independently re-exercised.
- In the mixed Python/TypeScript workspace, unqualified
  `jaunt clean --orphans --json` crashed with
  `AttributeError: Namespace has no attribute jobs` during its Python status
  preflight, while `--language ts` completed successfully. Against clean source
  HEAD `2b168db`, the unqualified command now passes with no removals. This
  directly confirms the mixed-clean `args.jobs` fix.
- Running the adopter's broad `uv sync --all-packages ...` replaced the editable
  1.7.9 source install with the lockfile's published 1.7.8. The old mixed-clean
  crash therefore appeared to regress until the editable source was restored.
  This is expected environment reconciliation rather than a Jaunt correctness
  defect, but it is easy to misdiagnose during local release testing. A clear
  `jaunt doctor` provenance line showing the resolved executable/module path
  and locked requirement, plus a documented workflow for restoring or
  preserving editable installs across `uv sync`, would reduce that friction.
- The same downgrade exposed a more consequential release-handoff gap. With
  published Jaunt 1.7.8 and `@usejaunt/ts` 0.1.0-alpha.7 restored, `status`
  classified all 19 implementations as structurally stale even though the
  committed artifacts had just passed under 1.7.9 / 0.1.0. The supported
  no-write `migrate --language ts --json` planner classified all 19 as
  `model-rebuild`; none qualified for `free-recompose` or `free-restamp`.
  Reconstructing the exact old-tool request keys found zero of 19 implementation
  cache hits and zero of 30 retained battery cache hits. A targeted old-version
  build would therefore make a paid call, and an old-version test refresh would
  regenerate every battery.

  The cache key does not name the Jaunt versions directly, but it includes the
  full analyzer contract and structural/API/battery fingerprints, which change
  across this stable-to-alpha tool boundary. Please provide a supported
  model-free forward/downgrade restamp for already validated, semantically
  unchanged artifacts when the worker can prove compatibility. If downgrade
  reuse cannot be made safe, release handoff guidance should explicitly say
  that artifacts produced by an unpublished release candidate must not be
  committed until matching Python and npm packages are available, and should
  provide a deterministic way to verify that the adopter lock matches the
  artifact provenance before any build can spend.
- A follow-up under the restored editable 1.7.9 / source `@usejaunt/ts` 0.1.0
  isolated the current all-19 rebuild to the semantic environment. For
  `auth/return-to`, the current analyzer matched the committed sidecar's route,
  symbols, options, type declarations/imports, context docs, dependencies,
  prose digest, and toolchain fingerprint. The only model-facing difference
  was `semanticEnvironmentDigest` (`89c2da...` versus `c41e0a...`), which then
  cascaded into `structuralDigest` and `apiDigest`. The supported migration
  planner still classified all 19 modules as `model-rebuild`.

  Type-environment capture currently hashes each whole lockfile while
  normalizing only Jaunt entries. Thus unrelated pnpm peer/snapshot rewrites
  are sufficient to invalidate every governed module even when its authored
  contract and imported declarations are unchanged. Restoring the original
  staged lock and doing a frozen install also remained stale; because
  `tsconfig.app.json` includes `vite/client`, the installed declaration/peer
  closure is the remaining likely historical input, but the sidecar exposes
  only an aggregate digest and cannot identify the changed record. Treedocs
  metadata and Jaunt's ignored build output were ruled out.

  Please scope lockfile compatibility to packages in the resolved declaration
  closure (or otherwise prove irrelevant lock changes), expose per-record
  semantic-environment diffs in `status`/`migrate`, and allow a worker-proven
  model-free restamp when authored contracts and relevant declarations are
  unchanged. Without that evidence, the only supported recovery is a paid
  rebuild or recreating an opaque historical `node_modules` state.
- Final resolution narrowed that conclusion. Restoring the original pnpm 11.5
  lock and the required `minimumReleaseAgeExclude` reproduced the authored
  semantic environment: source Jaunt 1.7.9 / `@usejaunt/ts` 0.1.0 then reported
  all 19 TypeScript modules fresh. The earlier reconstruction had changed the
  package-manager environment and therefore was not equivalent to the one that
  produced the sidecars.

  A clean install of the published Jaunt 1.7.8 / `@usejaunt/ts`
  0.1.0-alpha.7 under that same pnpm 11 tree classified all 19 modules as
  toolchain-stale. The migration preview correctly offered 19
  `free-recompose` actions and zero model rebuilds. Its default 4 GiB worker
  exhausted the heap during `validateOverlay` request 16; setting
  `worker_heap_mb = 8192` let the preview complete. `migrate --apply` then
  refused the dirty adopter worktree, but `build --language ts` recomposed all
  19 modules with zero API calls, zero tokens, and $0 cost. With Codex auth
  disabled, `test --language ts --no-build` reheadered and ran all 30 retained
  batteries with zero calls; all passed, followed by a clean final `check`.

  The compatibility path is therefore sound once the exact package-manager
  environment is restored. Remaining DX asks are to expose pnpm/package-manager
  semantic provenance and warn before analysis under the wrong package manager;
  avoid cumulative overlay heap growth by batching or resetting worker/compiler
  state; and permit deterministic migration apply on a dirty tree when writes
  are restricted to declared generated artifacts, or explicitly document
  `build` as the dirty-worktree fallback.
- Mixed-workspace freshness reporting then produced a dangerous false positive.
  An unqualified status payload exposed 19 Python modules through its top-level
  `fresh` collection while all 19 TypeScript modules remained structurally
  stale under `targets.ts`. The injected workspace summary reduced that to
  "19 fresh" and omitted the TypeScript target entirely, so we initially
  mistook the output for recovery from the semantic-environment blocker.

  Please make mixed top-level freshness either a true aggregate or explicitly
  language-qualified, and have hook/plugin summaries consume every target and
  report stale, unbuilt, and invalid counts per language. A partial probe must
  say that it is partial rather than presenting one target's fresh count as
  workspace health.
- The successful full test JSON was about 12,862 lines, roughly 135,000 tokens,
  because it emits every individual Vitest case; the detailed runner payload is
  also exposed both top-level and under `targets.ts`. No compact JSON or
  report-file option is currently exposed by the test CLI. Please add a summary
  mode that keeps aggregate counts, failures, battery states, cache, and cost
  inline, with an optional file for complete per-case results.

## 2026-07-15: local 1.7.9 replay validates targeted repair, with two cache/diagnostic gaps

I temporarily installed the prepared Python 1.7.9 source editable from
`/home/diwank/github.com/creatorrr/jaunt-ts-0-1-0` and linked its locally built
`@usejaunt/ts` package into the adopter checkout. The tracked dependency
manifests remained unchanged.

Status correctly classified all 19 TypeScript implementations as
toolchain-stale. Build then recomposed and refroze all 19 in 181.75s, with
3,421,004 KB peak RSS and zero API calls or cost. A targeted
`brief-blocks-core` test generated and staged both batteries with all 85
protected tests green. It used two calls, 780,130 tokens, and $1.678826 over
342.73s, with 1,844,852 KB peak RSS. Both battery outcomes reported
`attempts=1`, `retry_count=0`, and no diagnostics. The atomic accepted-subset
manifest write appears sound in this replay.

Two independent source-review findings remain:

1. The default validation path requests `redact_derived=True` in
   `tester.py:3598-3611`. Redaction strips diagnostic messages at
   `tester.py:2141-2159`, while `_runner_validation_errors` at
   `tester.py:3217-3242` requires both code and message and can therefore
   return only its generic fallback. The runner does supply messages at
   `runner.ts:109-128`, but the regression mocks exercise unredacted
   diagnostics. Please preserve bounded diagnostic messages through the
   default redaction path, and add a regression test that uses the production
   redaction setting.
2. An incompatible cached battery reports `attempts=0` at
   `tester.py:3679-3688`, so it is not added to `pending_cache_writes` at
   `tester.py:3888-3892`. Combined isolation filters only pending new writes at
   `tester.py:3787-3796`; it does not evict the rejected cached entry.
   `generate/request_cache.py:96-113` will therefore return the same
   incompatible hit on later runs unless the adopter uses `--force` or clears
   the cache. Please invalidate or replace a cached response that fails the
   current validator.

The full replay against adopter commit `662275e` selected 38 batteries: two
were already fresh from the targeted run, and 36 were missing or stale. The
six-job command remained concurrent, but took 26m09.95s and exited 3, with
1,915,336 KB peak RSS. It made 59 API calls (36 initial candidates plus 23
retries), used 23,144,390 tokens, and cost $48.539620. The response cache
reported zero hits and 36 misses.

Sixteen valid candidates reached a green combined `stage_preflight` and were
preserved in the response cache; 20 failed after the maximum two attempts, and
the other two were the pre-existing fresh batteries. Three retries repaired
their candidates and staged successfully: both `auth/user-roles` tiers and the
`digest-model` derived tier. The other 20 retries failed. This accepted-subset
cache preservation is a material improvement over alpha.7, but no battery
artifact landed because generation did not complete; the top-level
`generated` list remained empty.

Every retry reason and every top-level generation failure was exactly
`TypeScript test overlay validation failed without a diagnostic`. This
directly confirms the redaction finding above: all 23 repair calls were blind
to the actual compiler or policy defect. The run was correspondingly slower
than alpha.7's 10m52.43s because each live candidate now performs a protected
overlay and 23 additional model calls ran, even though six-way generation
concurrency remained active.

## 2026-07-15: 1.7.8 parallelizes battery generation but still loses valid work

A full `jaunt test --language ts --no-build --jobs 6 --json --progress plain`
run over 19 intents and 38 batteries used true six-way concurrency. It finished
in 10m52.43s instead of the roughly 55-minute serialized 1.7.7 run, with
1,663,552 KB peak RSS. The run made 38 calls, used 10,309,128 tokens, and cost
$21.769356. The concurrency fix is material.

The command still exited 3 at the final overlay typecheck with `generated=[]`.
All 38 battery states were `rejected`. Exactly 38 API calls means no candidate
retry occurred. The actual diagnostics were confined to six modules:

- TS2532 in the example batteries for `brief-blocks-core`, `onboarding-steps`,
  and `digest-model`;
- TS2352 in the `meeting-readiness` example battery; and
- `JAUNT_TS_TEST_DYNAMIC_LOADER` in batteries for `brief-blocks-core`,
  `brief-draft-proposal`, `entity-mention-menu`, `onboarding-steps`, and
  `meeting-readiness`.

This does not realize the advertised per-battery compiler validation and retry
for these failure classes. The final atomic overlay rejection also still
discards every unaffected artifact. Please:

- validate each candidate against the exact final overlay project, including
  the dynamic-loader policy, before accepting it;
- expose per-candidate outcomes and retry reasons in test JSON; and
- preserve response-cache entries and commit unaffected valid batteries after
  a late cross-battery failure, while keeping the final artifact update atomic
  where paths actually conflict.

A targeted rerun of the unaffected `auth/return-to` intent confirmed both
losses. It reported `cache.hits=0` and `cache.misses=2`, launched two fresh API
calls, and spent another 510,101 tokens / $1.072072 over 252.41s, with
1,604,312 KB peak RSS. The rerun passed and staged both batteries. The failed
full run therefore preserved neither reusable response-cache entries nor valid
artifacts for this unaffected intent.

The installed 1.7.8 source shows the validation gap. The per-battery compiler
validator is defined in `jaunt/typescript/tester.py:3522-3555`, but lines
3561-3574 pass it only as `cached_validator`. `generate/request_cache.py:96-102`
uses that validator on cache hits; fresh candidates at lines 126-130 retain
their original text-only validator from `tester.py:1272-1294`.
`generate/base.py:237-258` can therefore retry only the text checks. The
combined preflight at `tester.py:3701-3716` catches compiler and policy failures
too late, then returns before artifact staging at line 3750 and cache storage
at lines 3353-3364.

The narrow fix appears to be passing
`replace(request, validator=validate_candidate)` into fresh generation. Please
also return bounded, exact compiler or policy diagnostics from that validator
instead of the generic message at `tester.py:3553-3555`, so retries can repair
the actual defect.

## 2026-07-15: 1.7.7 discards every successful battery after a late typecheck failure

A full 19-intent `jaunt test --language ts --jobs 6` run generated all 38
example and derived battery candidates serially over roughly 55 minutes. It
used 10,863,354 tokens and cost $22.90245. The final workspace overlay
typecheck then failed, and the command committed neither generated batteries
nor response-cache entries. No generated batteries landed.

The final diagnostics mixed three failure classes:

- generated batteries for `brief-blocks-core`, `brief-draft-proposal`,
  `entity-mention-menu`, `onboarding-steps`, and `meeting-readiness` used
  forbidden dynamic loaders that did not typecheck;
- the briefs-navigation batteries invented properties and used unsafe partial
  casts, producing TS2339 and TS2352 diagnostics; and
- the existing native `src/pure/home-data.test.ts` had TS2591 in Jaunt's
  overlay because the child `tsconfig.jaunt-test.json` `exclude` replaces,
  rather than extends, the inherited parent `exclude`.

Jaunt did not compile/analyze and repair each battery after generation. It
first surfaced these candidate defects in the final all-workspace preflight,
after all 38 paid generation calls had completed.

The source confirms why all successful responses were lost.
`src/jaunt/typescript/tester.py:3341-3544` holds generation results in the
process-local `pending_cache_writes` list. The final overlay typecheck returns
at lines 3605-3633 on failure. `commit_test_files()` and
`commit_test_cache()` are not defined until lines 3654-3704, so neither the
candidate files nor their cache entries can be committed on that path.

Please treat this as an urgent resumability and transaction-boundary issue:

- commit each successful model response to the response cache after validating
  that individual response, independently of the final artifact transaction;
- honor `--jobs` with bounded battery-generation concurrency;
- typecheck and repair each battery before advancing to the next work unit; and
- limit the final atomic transaction to candidates that already passed their
  per-battery validation.

Thirty-eight successful, paid model responses must not be discarded because a
few candidates or an accidentally included native test fail the final
workspace typecheck.

## 2026-07-15: 1.7.7 TypeScript test jobs do not parallelize battery generation

With 19 fresh dashboard specs, `jaunt test --language ts --jobs 6 --json
--progress plain` visibly kept only one paid Codex child active at a time. The
source matches the observation: `src/jaunt/typescript/tester.py:3431-3523`
walks the prepared example and derived requests with a plain `for` plus
`await generate_request_cached(...)`. The `jobs` value reaches the
implementation build and held-out implementation repair, but not this battery
loop. The build path does use a bounded semaphore and `asyncio.gather` in
`src/jaunt/typescript/builder.py:1500-1612`.

Launching targeted test commands in parallel is not a safe replacement. Each
process owns a separate analyzer worker, but all processes share unlocked
response-cache files and process-local artifact transactions. A full command
and a targeted command can overlap the same battery paths; even disjoint
targets still share `.jaunt` state without an interprocess coordination
contract.

Please make TypeScript battery generation honor `--jobs` inside one process,
with dependency-safe validation and landing. Also commit or durably stage each
validated battery so a long 19-spec, 38-battery refresh can resume instead of
losing all successful work when a late request fails. Plain and JSON progress
should report each completed battery and tier as it lands.

## 2026-07-15: 1.7.6 fixes full-workspace OOM, but sync typechecks authored marker bodies

The full 19-spec `status --language ts` now succeeds without a heap override:
70.30 seconds at 3,817,384 KB peak RSS. This is a material improvement over
1.7.5's repeated worker OOMs. Full `sync --language ts` likewise completed its
workspace analysis in 82.63 seconds at 3,755,892 KB peak RSS, and its
independent batches successfully landed eight API mirrors before the command
exited 2.

The remaining failure is that sync compiles authored `index.jaunt.ts` marker
bodies under `noUnusedParameters`. `brief-blocks-core` emitted TS6133 for
`raw`, `blocks`, `patch`, and `opts`; `source-health` emitted TS6133 for
`status`, `source`, and `input`. These are governed specs whose
`jaunt.magic()` marker bodies necessarily do not read their public parameters,
and the authored spec files were already excluded from the production
project's normal include.

Please suppress TS6133 only for governed stub parameters, or synthesize marker
uses in the compiler overlay. Public API parameters should not need underscore
renaming, and adopters should not have to disable `noUnusedParameters` in the
production project. The fact that eight independent mirrors still landed is
good evidence that the new batching and failure isolation are working.

One smaller consistency issue: `jaunt instructions` reported all 19 modules
fresh immediately before authoritative status found 18 stale and one unbuilt.
Please have instructions use the same freshness source, or label its summary as
cached when it is not authoritative.

## 2026-07-15: 1.7.5 follow-up conformance failure again skips retries

After adding an explicit no-self-import prompt, the targeted `detect-core`
build again made exactly one API call instead of using the advertised retry
budget. It generated private input types that narrowed optional
`content_blocks` to required, causing repeated TS2322 conformance failures for
`factRows`, `hasFactsView`, and `detectSectionViews` in both `index.ts` and
`.jaunt-conformance.ts`; zero artifacts landed.

The call used 745,204 prompt tokens, 13,683 completion tokens, 758,887 total
tokens, and cost $1.599872. Across the two failed attempts, this small module
spent 1,750,324 tokens and $3.683570 without producing an artifact.

Please retry or repair type-narrowing conformance failures, deduplicate
candidate and conformance diagnostics, and ensure generated private types
preserve source optionality exactly.

## 2026-07-15: FEEDBACK-REPLY release state is already stale

`FEEDBACK-REPLY.md` still says PR #90 is unmerged and Jaunt 1.7.5 / TypeScript
alpha.4 are unpublished, while both releases are live and installed here.
Please update the release-state preamble, or date/version-stamp replies so a
reader can distinguish historical status from current availability.

## 2026-07-15: 1.7.5 targeted conformance failure still skips its retry budget

A targeted rebuild of the small pure `detect-core` module made one API call:
974,633 prompt tokens, 16,804 completion tokens, 991,437 total tokens, and an
estimated $2.083698. The candidate imported its own
`__generated__/index.ts`, failed `JAUNT_TS_GENERATED_PRIVATE_IMPORT`, and
landed zero artifacts.

The 1.7.5 reply says compile and conformance failures now retry within the
configured budget, but this failure returned after that single call with no
retry. Please automatically rewrite or retry same-module generated-private
imports, report the retry count and reason clearly in JSON, and substantially
reduce target prompt size: nearly one million prompt tokens is disproportionate
for this module.

## 2026-07-15: 1.7.5 target-scoped sync still emits strict-project-invalid mirrors

Target-scoped `sync` now avoids the full-workspace OOM, but strict TypeScript
placeholder and API-mirror compatibility is still incomplete. A type-only
import present in a Jaunt spec but unused by its declarations is copied into
`__generated__/index.api.ts`, where it triggers TS6196 under
`noUnusedLocals`. Every requested target in the same sync invocation then
fails with repeated diagnostics attributed to that unrelated mirror.

Separately, an unbuilt module's generated API imports a runtime helper used
only by the eventual implementation, triggering TS6133.

Please prune mirror imports that are unused by public declarations, including
runtime dependencies needed only by the implementation; keep placeholders
clean under strict projects; deduplicate diagnostics and attribute them to the
blocking module; and prevent one unrelated mirror failure from poisoning
independent target syncs.

## 2026-07-15: 1.7.5 fixes protocol batching, but overlay validation still OOMs

With the `mem-mcp-c` dashboard's 19 TypeScript specs, both the full dashboard
project and a strict project limited to `src/pure` now get past the prior
16 MiB worker-response limit. This confirms that 1.7.5's protocol batching is
working.

However, `jaunt status --language ts` then drives the worker to roughly
4.0-4.1 GiB and OOMs in `validateOverlay` / `module-overlays`. The worker
restarts, replays the same work, and OOMs again. `NODE_OPTIONS` appears to be
stripped from the worker environment, and no supported worker heap setting was
found.

Please consider:

- batching overlay validation itself and releasing program/AST state between
  batches;
- a supported worker heap setting or `NODE_OPTIONS` pass-through;
- diagnostics identifying the largest project or dependency closure; and
- avoiding automatic replay after a deterministic OOM.

## 2026-07-15: 1.7.4 TypeScript migration exposed mixed-clean and discovery gaps

### Addendum: advertised compile-repair retries were not used

The first `plan-state` build made 1 API call, used 1,094,962 tokens
(981,142 cached prompt), and cost $2.241098. It then failed conformance with
TS2339, `Property status does not exist on type never`, in both the candidate
and `.jaunt-conformance`; zero artifacts landed. Status and the build plan
advertised `max_attempts_per_unit=3`, but no compile-repair retry occurred.

Please retry candidate compile/conformance failures before aborting, report
clearly why an advertised retry budget was not used, and reuse and repair the
failed cached candidate instead of regenerating it.

### Addendum: one invalid candidate aborts independent modules

A four-module briefs build made 4 API calls, used 1,708,130 tokens
(1,431,991 cached prompt), and cost $3.580972. One
`brief-draft-proposal` candidate failed `JAUNT_TS_GENERATED_PRIVATE_IMPORT`
after importing its own `__generated__/index.ts`. The other three unrelated
modules were then marked `JAUNT_TS_COMPONENT_ABORTED` because the owner/reference
component failed, so zero artifacts landed.

Please isolate independent modules/components, retain or park successful
candidates instead of discarding them, retry or repair only the failing
candidate, and report per-module phase and candidate outcomes.

### Addendum: full-workspace bootstrap still requires manual config slicing

With 19 configured TypeScript specs, both unified `status` and `sync` fail with
`TypeScript worker response exceeds 16777216 bytes`. Passing `--target` does
not reduce analysis scope: it still analyzes the entire configured workspace
and fails with the same response-size error. A broad root also reproduces the
Vitest provenance problem described below.

Strict unbuilt placeholders then produce sibling TS6133/TS6192 diagnostics.
Successful adoption has required repeatedly rotating committed
`jaunt.toml` source/test roots and the TypeScript project `include`, syncing or
building a small slice, then widening again. Even after building two blocking
placeholders, a six-spec dashboard slice initially failed until it was narrowed
further.

Please provide:

- truly target-scoped analysis and bootstrap;
- automatic dependency-ordered batching for sync, build, and test;
- unbuilt placeholder code compatible with `noUnusedLocals` and
  `noUnusedParameters`;
- chunked/streamed worker responses, or a larger configurable protocol limit;
- a supported temporary project/overlay mechanism that does not mutate
  committed configuration.

A larger frontend migration found four remaining adoption issues.

1. In the mixed Python/TypeScript workspace, `jaunt clean --orphans` crashes before cleaning: `cmd_clean` -> `_mixed_python_preflight` -> `cmd_status` raises `AttributeError: 'Namespace' object has no attribute 'no_infer_deps'`. The language-qualified `jaunt clean --language ts --orphans` succeeds. Please populate the status-only argparse fields in mixed-clean preflight and add a mixed-target clean regression.
2. Broadening `[target.ts].source_roots` to the pure frontend tree, which also contains ordinary co-located native Vitest tests, reports `vitest is not declared by .../package.json` even though `vitest` is in that package's `devDependencies`. If generated production code must depend only on `dependencies`, keep that policy, but identify the offending native-test import and explain that `devDependencies` do not authorize production candidates. Better, exclude ordinary tests from implementation provenance unless selected as Jaunt test intent. The current workaround is explicit per-spec roots.
3. Plugin doctor reported Jaunt status unavailable and the TS worker/compiler unavailable immediately after `jaunt instructions` reported 19 fresh modules and direct status had worked. This looks like a timeout or false negative on the slow full TS project. Doctor should distinguish timeout from missing worker/compiler, use a lighter handshake or honor configured worker deadlines, and avoid turning one status timeout into multiple unavailable diagnoses.
4. The TS alpha grammar cannot preserve module-level runtime constants. `brief-blocks` therefore needs a handwritten `brief-blocks.ts` wrapper exporting `BLOCK_META_FORMAT` plus a generated `brief-blocks-core`, while wrapper/core patterns multiply configured roots. Please support explicitly preserved runtime constants, or document wrapper/core as the canonical migration pattern and discover the paired core without one literal root per module.

## 2026-07-15: 1.7.4 fixes full-project overlays, but reused batteries still regenerate

The alpha.2 to alpha.3 retest confirmed model-free implementation reuse. With
the narrow project, status classified the helper as `toolchain` and build
finished in 3.5 seconds with zero calls or tokens. Switching to
`apps/dashboard/tsconfig.app.json` made status `fingerprint`; it completed in
68.33 seconds at 4,049,124 KB maximum RSS. The full-project build completed in
63.63 seconds at 4,047,836 KB with no redirect assertion and again used zero
calls or tokens.

Both build JSON results reported the helper under `refrozen`, not the documented
`recomposed` field. More importantly, targeted `jaunt test` regenerated both
unchanged batteries in two calls: 256,955 tokens, $0.534484, and zero cache
hits. The protected runner passed eight example cases and reported zero
derived cases. This appears inconsistent with the reply's promise to reheader
matching batteries when every target is proven recomposed; the test path may
not recognize a `refrozen` implementation as recomposed.

Please align the build JSON vocabulary with the documentation and let this
model-free reuse state authorize battery reheadering. The cached failed-candidate
retry was not exercised in this run because the full-project change took the
deterministic fingerprint/refreeze path.

## 2026-07-15: 1.7.3 full-project overlay reuse crashes after a successful candidate validation

The configurable worker deadlines work: with
`projects = ["apps/dashboard/tsconfig.app.json"]`, a targeted 1.7.3 status run
completed instead of timing out. A repeat took 63.07 seconds and about 4.05 GB
maximum RSS. It reported the one TypeScript module as structurally stale and
all 19 Python modules as fresh.

The targeted build then generated a valid nine-line helper, but failed after
one model call with:

```text
TypeScript worker INTERNAL_ERROR: Debug Failure. False expression: Host should
not return a redirect source file from `getSourceFile`
```

That call used 216,509 tokens and cost $0.441226. The implementation, API
mirror, facade, sidecar, and both batteries were untouched; their timestamps
and hashes still match the prior 1.7.2 / alpha.1 build. The old sidecar still
names `tsconfig.jaunt.json`, worker `0.1.0-alpha.1`, and tool `1.7.2`.

The response cache tells us where the crash happened. A new cache entry was
written at the failure time with exactly 215,141 prompt tokens, 1,368
completion tokens, and the generated candidate. In the 1.7.3 builder, a model
result is cached only after its request validator accepts it. The builder then
runs the same candidate through a second, transaction-level `validateOverlay`.
`OverlayProgramCache` supplies that second call with `oldProgram`, and
`overlayHost.getSourceFile` returns `oldProgram.getSourceFile(...)` directly.
On this pnpm/Vite project, one of those prior files is a TypeScript redirect
source, which violates TypeScript 5.9's reuse invariant and triggers the exact
assertion above.

Restoring the narrow `tsconfig.jaunt.json` made the implementation build pass,
but the project-identity change invalidated the full-project response cache.
The retry reported zero cache hits and spent a second model call: 187,359
tokens and $0.382086. `jaunt test` then regenerated both alpha.1 batteries in
two more calls (393,315 tokens, $0.811356); the protected runner reported seven
passing example cases and zero derived cases. Across the failed full-project
attempt and the workaround, the upgrade used four calls, 797,183 tokens, and
$1.634668. An independent first-build review found no behavior drift, and
`jaunt check`, dashboard typecheck, lint, and the full dashboard test suite all
passed.

Please unwrap redirect sources before returning a reused `SourceFile`, or let
the compiler host recreate them. Add a regression that runs two consecutive
overlay validations against a pnpm project large enough to contain package
redirects. `INTERNAL_ERROR` should also report the worker phase and preserve
the first successful validation evidence. A retry with the same project should
reuse the accepted candidate, but changing projects for the workaround did not.

## 2026-07-14: Jaunt 1.7.2 TypeScript frontend retest

The 1.7.2 retest confirmed several fixes. Before disabling TypeScript auto
skills, `jaunt status --json` previewed the full 77-file, 315,932-byte npm
skills surface. `[target.ts].auto_skills = false` removed that fanout without
disabling Python skills. Strict `noUnusedLocals` and `noUnusedParameters` now
pass without Jaunt-specific overrides, and the exported
`shouldConsumeChatSeed.name` regression passes. Root `jaunt status` and
`jaunt check` are clean, and all 1,119 frontend tests pass.

Two issues remain:

1. The intended narrow source root with the normal
   `apps/dashboard/tsconfig.app.json` no longer produces the old provenance
   flood, but it repeatedly times out in `validateOverlay` at the fixed
   30-second `WorkerClient` deadline. This reproduced in sequential runs. An
   earlier concurrent `status` plus `doctor` run instead timed out during
   initialization; that is a separate concurrency result, not evidence for the
   `validateOverlay` failure. There is no config or environment override for
   the deadline, so the trial still needs a dedicated narrow tsconfig. Please
   make the deadline configurable or adaptive and report phase timings when a
   worker request times out.
2. Upgrading from Jaunt 1.7.1 / `@usejaunt/ts` 0.1.0-alpha.0 to 1.7.2 /
   0.1.0-alpha.1 made the unchanged nine-line helper structurally stale and
   both batteries fingerprint-stale. Rebuilding required three model calls,
   625,706 total tokens, and $1.287910: implementation was one call, 271,452
   tokens, and $0.554892; tests were two calls, 354,254 tokens, and $0.733018.
   Can compatible toolchain-only upgrades revalidate and recompose existing
   candidates without model calls?

The shipped documentation also has two stale claims. The TypeScript guide says
infrastructure-failed candidates are cached, while the shipped code and tests
intentionally do not cache them. `FEEDBACK-REPLY.md` still says the PR is open
and the packages are unpublished, though 1.7.2 and 0.1.0-alpha.1 are live.

## 2026-07-14 — Tiny TypeScript adoption generated a broad skills surface

The first TypeScript build for a zero-import, one-function spec owned by
`apps/dashboard` auto-generated 77 `npm-*` skill files, one for every direct
dashboard dependency: roughly 0.8 MB and a large review and commit surface for
a tiny adoption. This is documented default behavior, and current project
policy commits generated skills, so we kept them. Please support
dependency-reachable skill generation per target/spec, a per-target auto-skills
switch (shared `[skills].auto=false` would also disable Python skills), and
pre-build plan visibility into the generated file count and total size.

The same trial found a smaller repository-hygiene gap: `jaunt test` created
root `.jaunt-vitest-cache/`, but init/migration had not added it to `.gitignore`,
so we added `.jaunt-vitest-cache/` manually. Jaunt should seed and check this
ignore alongside `.jaunt/`.

## 2026-07-14 — Overlapping TypeScript roots orphan freshly generated batteries

With `[target.ts].source_roots` and `test_roots` set to the same narrow
directory, `jaunt test` succeeded and wrote co-located
`__generated__/index.example.test.ts` and `index.derived.test.ts`. An immediate
root `jaunt check` classified both fresh batteries as orphans, and
`jaunt clean --orphans --language ts` removed them. Moving authored test intent
to `apps/dashboard/jaunt-tests/...`, adding explicit `test_projects`, and
regenerating fixed routing; the final mixed Python and TypeScript check was
green. The protected separate-root run reported eight passed example cases and
zero derived cases (passed derived cases are redacted there). After adding
`jaunt-tests/**/*.{test,spec}.{ts,tsx}` to the normal Vite include, Vitest
passed eight example plus 12 derived cases: 25 total including five native.

That route-only correction forced another 2 generation calls, 434,348 total
tokens, and $0.894958. Please either support co-located source/test roots or
reject their overlap before generation and model spend, and reuse or move fresh
battery candidates when only the test-root route changes instead of charging
for generation again.

## 2026-07-14 — Generated TypeScript functions expose internal names

The first implementation build with Jaunt 1.7.1 and `@usejaunt/ts`
0.1.0-alpha.0 succeeded in one call for $0.637042 with no advisories, and its
native behavior and type checks were otherwise faithful. Review found one
observable compatibility drift: handwritten `shouldConsumeChatSeed.name` was
`shouldConsumeChatSeed`, while the generated export is a const alias of named
`__jaunt_impl_shouldConsumeChatSeed`, so `.name` exposes the internal name. No
current consumer depends on it, but the composer should preserve exported
function names or explicitly document this reflective compatibility boundary.

## 2026-07-14 — Opaque TypeScript runner failure triggered a repair of correct code

With Jaunt 1.7.1 and `@usejaunt/ts` 0.1.0-alpha.0,
`jaunt test --language ts` generated two tiers, but both protected Vitest runs
returned only category `runner-protocol`, empty stdout/stderr, and zero test
records or diagnostics. Jaunt then automatically spent a third API call
repairing an implementation already proven correct; the rerun returned the
same opaque protocol failure and no batteries landed. Totals were 3 calls,
814,159 prompt tokens (702,176 cached), 6,445 completion tokens, 820,604 total,
and $1.679878: test generation used 2 calls/$1.169232 and the unnecessary repair
used 1 call/$0.510646.

The cause is now isolated. Invoking the protected runner manually against the
same native five-case test with
`vitestConfigPath=apps/dashboard/vite.config.ts` returned a collection failure;
omitting `vitestConfigPath` passed all five cases. Removing
`[target.ts].vitest_config` made `jaunt test` succeed: both batteries were
generated and all eight example cases passed (the derived tier contained zero
cases). That successful rerun used 2 calls, 414,597 total tokens, and $0.858696.

The primary bug is therefore the Python layer converting an underlying
config/collection error into opaque `runner-protocol`, then treating it as an
implementation defect eligible for repair. Please preserve the runner's
collection diagnostic and exit cause, never repair implementation for this
infrastructure category, persist or cache generated candidates across reruns,
and document that application Vite config should be omitted for pure batteries
unless their tests specifically require it.

## 2026-07-14 — TypeScript mirror validation conflicts with strict unused checks

With Jaunt 1.7.1 and `@usejaunt/ts` 0.1.0-alpha.0, `sync` under a normal strict
Vite `tsconfig` with `noUnusedLocals` and `noUnusedParameters` enabled rejected
Jaunt's synthetic
`.jaunt-mirror-check.ts` bindings (`__api_from_spec_0`, `__spec_from_api_0`, and
`__facade_value_0`) and every intentionally unused magic-stub parameter as
TS6133. The workaround was a dedicated Jaunt `tsconfig` disabling those two
checks while the real app project retained them. Synthetic worker validation
should consume or suppress its own bindings and stub parameters so strict
projects do not need a weakened analysis configuration.

## 2026-07-14 — TypeScript provenance rejected workspace-local imports outside the target

With Jaunt 1.7.1 and `@usejaunt/ts` 0.1.0-alpha.0, doctor/status against the
`mem-mcp` dashboard's `tsconfig.app.json` failed before model spend with
hundreds of provenance errors, even though the targeted spec had zero imports.
Jaunt classified the configured `@/*` path alias (for example `@/assets`) as an
undeclared npm package, and treated existing tests that import sibling app API
modules as undeclared `mem-dashboard-api` dependencies of the frontend package.
Repeated copies of the same errors made the output enormous and eventually
truncated.

Please resolve `tsconfig` path aliases as local source, offer scoped/project
diagnostics that do not validate unrelated files for a one-module target,
deduplicate identical provenance failures with counts, and clarify how
workspace-local package imports should be authorized. The trial proceeded with
a dedicated narrow `tsconfig` as a workaround.

## 2026-07-14 — Successful 1.7.1 build still emitted a non-canonical stub

The successful targeted `temporal` build emitted
`apps/memory-api/mcp_memory_server/temporal.pyi`, then the repo's required
pre-commit formatter failed with exactly:

```text
Would reformat: apps/memory-api/mcp_memory_server/temporal.pyi
```

`uv run ruff format` changed that one file, while `jaunt status` still reported
every module fresh. This narrows the release reply's Ruff-clean artifact claim:
the stub may be lint-clean, but it is not canonical under the owning repo's
required Ruff formatter, and Jaunt does not surface the byte mismatch. Emitted
provenance stubs should use the same deterministic formatting as the owner, or
`jaunt check` should report the mismatch.

## 2026-07-14 — One local contract fix rebuilt a large module for $20.86

After we documented the invalid-year `ValueError` behavior only in
`parse_temporal_reference`, status marked the single
`mcp_memory_server.temporal` module prose-stale and warned of at most 51
generation attempts. The build succeeded, but ran 16 component generations
plus the monolithic fallback: 35 API calls, 9,998,887 prompt tokens (8,695,646
cached), 107,823 completion tokens, an estimated $20.860358, and zero Jaunt
cache hits. Its only advisory concerned the inferred `_coerce_instant`, not the
changed parser contract.

This is a high-cost locality problem. A behavior clarification in one function
regenerated every function in a large module. Can prose-diff symbol targeting
preserve unaffected generated components, or can the semantic refreeze path
retain them when their contracts did not change? The pre-build plan should also
make the component fan-out and conditions for monolithic fallback explicit so
adopters can see this cost before starting the build.

## 2026-07-14 — Fresh regeneration exposed an underspecified invalid-year contract

A Jaunt 1.7.1 regeneration of `mcp_memory_server.temporal` removed the prior
generated implementation's `ValueError` guards around four-digit year parsing
and year-range parsing. Inputs such as `"0000"` matched the documented forms, but
now reached `datetime(year, ...)` directly and raised instead of returning
`None`. The generated module was fresh and `jaunt check` was clean; PR review
found the behavior change.

This is not evidence of a compiler defect. The spec described valid input forms
and one invalid calendar-date case, but never said what to do with an invalid
Gregorian year. Both implementations were plausible readings of that contract.
The adoption lesson is to characterize established boundary and failure
behavior before a structural regeneration, and to review a fresh generated diff
as a semantic change even when every Jaunt gate passes.

Jaunt could make that review cheaper when a previous artifact exists. A targeted
differential advisory that calls out removed exception guards or changed failure
paths, or a prompt to add characterization cases for affected branches, would
surface this class of risk without claiming that Jaunt can prove semantic
equivalence.

## 2026-07-14 — 1.7.1 provenance stubs are lint-clean but not formatter-stable

After 1.7.1 deterministically re-emitted the workspace stubs, with no model
calls, the repo-required `uv run poe check` ran `ruff format .` and reformatted
exactly six Jaunt-owned provenance files:

- `apps/memory-api/mcp_memory_server/temporal.pyi`
- `memory_store_utils/{compression_utils,deixis,entity_text}.pyi`
- `memory_store_telemetry/dbos_instrumentation.pyi`
- `memory_store_postgres/pool.pyi`

`jaunt status` and `jaunt check` still reported all 17 modules fresh afterward
because stub freshness does not track the rendered bytes. Thus 1.7.1 stubs now
pass the adopter's Ruff lint rules, but they are not stable under its required
formatter: the normal repo gate modifies generated artifacts despite Jaunt's
"never edit generated files" guidance, and Jaunt's drift gate does not notice.

The likely cause is target-version inference: the repo formats for Python 3.13,
while Jaunt's isolated stub validation uses Python 3.12. That is an inference,
not yet a confirmed root cause. The emitter should honor the owning package's
Ruff/target-Python configuration (or otherwise emit formatter-canonical bytes),
and a deterministic post-build check should catch this without a model call.

## 2026-07-13 — 1.7.0 Codex plugin: resolver works; doctor crosses workspace and host boundaries

First package-adoption pass with the installed Codex plugin. The resolver
selects the expected workspace Jaunt:

```bash
bash ~/.codex/plugins/cache/jaunt-codex-plugins/jaunt/1.1.0/scripts/resolve-workspace.sh \
  --run "$PWD" --version
# jaunt 1.7.0
```

Two `doctor` findings need narrower boundaries:

- **Workspace discovery includes an unrelated Claude worktree.** From the
  `mem-mcp-c` root, `JAUNT_WORKSPACE_ROOT="$PWD" .../scripts/doctor.sh`
  reported `.claude/worktrees/agent-af98044930db9d458` as unavailable because
  its environment could not import `numpy`, followed by the actual root
  workspace's status. That nested worktree is not part of the root adoption
  run. Doctor should skip tool-managed worktrees such as
  `.claude/worktrees/**`, or report them in a separate section that does not
  read as root-workspace health.
- **Duplicate-hook detection conflates Claude and Codex hosts.** The same run
  told us to remove the guard in `.claude/settings.json` when the plugin hook
  is enabled. That file configures Claude Code's Edit/Write guard; the installed
  Codex plugin guards `apply_patch` through `hooks/hooks.json`. Enabling the
  Codex plugin does not replace the Claude hook. The duplicate check should be
  host-aware: compare `.claude/settings*.json` only with an enabled Claude
  plugin hook, and `.codex/*` only with an enabled Codex plugin hook.

### The plugin advertises doctor, but the 1.7 CLI has no doctor command

The Codex plugin exposes `jaunt:doctor`, and its workflow calls for a doctor
health check before a large build. In the Jaunt 1.7 `mem-mcp-c` worktree, the
corresponding resolver invocation failed:

```bash
bash .../scripts/resolve-workspace.sh --run "$PWD" doctor --json
# Jaunt 1.7 rejects "doctor" as an invalid command.
```

The resolver selected the installed Jaunt 1.7 CLI, but that CLI does not define
`doctor`. Either implement or restore the CLI command, or update the plugin
skill to use only the supported health-check commands.

### Dependency-only 1.7.0 upgrade rebuilt 12 modules for $38.06

After changing the Jaunt dependency from 1.6.2 to 1.7.0, with no spec edits,
`status --json` classified all 12 previously generated modules as
`structural`. `build --json --progress plain` rebuilt all 12 successfully, but
the build used:

- 41 API calls;
- 18,166,780 prompt tokens, 15,574,477 of them provider-cached;
- 215,440 completion tokens;
- an estimated $38.05708;
- `cache_hits: 0` in Jaunt's build summary.

Progress showed repeated attempts under the same module names.
`mcp_memory_server.temporal`, `memory_store_utils.entity_text`,
`memory_store_utils.lexical_match`, and `memory_store_utils.timing` each reached
attempt 3 for at least one generated component.

The context report also listed `skills_workspace_seeded` as 236,994 characters
(59,248 estimated tokens) for every module. That included small contracts such
as `coercion` (106 contract characters) and `db_errors` (140). The build
advisories were useful and concrete: `temporal` lacked dependency APIs,
`coercion` had a conflict between its never-raises promise and its narrower
exception handling, and `timing` did not supply the `MockTimer` contract.

For this adopter, a dependency-only minor-version upgrade was therefore a
41-call, $38 regeneration event. `status` correctly warned that implementation
rebuilds were coming; it did not expose the retry fan-out or the fixed
59,248-token skill context per module before the build.

### Advisory conflated missing model context with a missing workspace file

A successful default-mode build of `memory_store_reranker.client` emitted:

> The referenced `_context/dep_*.pyi` and
> `memory_store_reranker/errors.py` are absent from the provided workspace.

The package-local file
`packages/python/memory-store-reranker/src/memory_store_reranker/errors.py`
exists and is imported by both the governed source and generated
implementation. Package-local sibling modules used by the governed source
should be included in build context. If they intentionally are not, the
advisory should say "not provided to the model" rather than "absent from the
workspace."

### Multi-target build appeared to duplicate scheduled work

Help documents `--target` as repeatable. We selected five distinct stale
modules in one five-job build:

```bash
jaunt build --target memory_store_embeddings.client \
  --target memory_store_reranker.client \
  --target memory_store_postgres.pool \
  --target memory_store_telemetry.tracing \
  --target memory_store_telemetry.dbos_instrumentation \
  --jobs 5 --include-target-tests --json --progress plain
```

Progress then showed five concurrent, independent generation attempts all
attributed to `memory_store_embeddings.client`, followed by repeated
`memory_store_postgres.pool` attempts, including retries. This may be duplicate
scheduling or incorrect progress attribution. The independent attempt/retry
lines strongly suggested multiplied model calls, so we interrupted the command
(exit 130) before taking that spend. No new generated artifacts were written;
all five modules remained structurally stale.

This was the clear low-hanging parallel build path: five independent modules
and five jobs. Once the multi-target invocation appeared to duplicate work, the
only safe fallback was one target per process, run sequentially. Launching
separate Jaunt processes concurrently could race on shared `.jaunt` state and
generated artifacts, so it was not an equivalent way to recover parallelism.

The resolved build plan should contain one work item per distinct selected
module when repeatable `--target` flags are used. Structured progress and final
cost output should likewise expose one attributable work item and cost per
module, so an adopter can distinguish retries from duplicate scheduling and
recover safe module-level parallelism through one multi-target command.

### Owner routing did not isolate `@jaunt.test` imports

The merged workspace initially used the documented owner-local routing shape:

```toml
test_roots = ["packages/python/memory-store-*/tests", "apps/memory-api/tests/unit"]
```

Four new test-spec files lived directly under four separate package `tests/`
directories, each with its own nearest `pyproject.toml`. This command failed:

```bash
jaunt test --target memory_store_embeddings.client --no-build --no-run --json
```

Jaunt tried to import every owner through the shared top-level `tests` name and
raised `ModuleNotFoundError` for `tests.embeddings_jaunt_specs`,
`tests.postgres_jaunt_specs`, `tests.reranker_jaunt_specs`, and
`tests.telemetry_jaunt_specs`. One owner's `tests` package/module shadowed the
others despite owner routing.

We worked around it by moving the specs into owner-unique top-level roots
(`embeddings_jaunt_tests`, `postgres_jaunt_tests`, `reranker_jaunt_tests`, and
`telemetry_jaunt_tests`) and listing those roots explicitly. The same test
command then reported the owners as OK.

That path-only move restaled all five implementation modules built with
`--include-target-tests`, even though test intent content was unchanged and the
generated module digests still matched their sources. Owner-isolated or
path-based test imports would remove the collision. `status`/`specs` should
also validate duplicate import names before generation, and target-test
context should remain fresh when only an equivalent test-intent path changes.

### Targeted `jaunt test` returned OK without a pytest result

We ran this for each of the four owner-unique Jaunt test modules:

```bash
jaunt test --target <owner_unique_test_module> --no-build --json --pytest-args=-q
```

All four commands reported `ok=true` for every owner, but included no pytest
result or output. Running pytest directly against the four generated batteries
collected tests and produced 5 passes plus 3 telemetry failures:

1. A generated test monkeypatched facade `_collapse_for_io`, but governed
   `_serialize_io_value` resolves sibling globals in the generated module, so
   the patch had no effect.
2. A generated test expected a truncated small-list representation to retain
   the full `value`, contrary to the contract.
3. A generated tracing implementation regressed the specified legacy behavior
   by using `response["usage"]` instead of `.get`; this was a real regression
   that the generated battery caught.

Root `ty` also treated the owner-unique generated test roots as source input.
The embeddings battery passed `str` and `float` values to
`_validate_dimensions(int | None)` without precise ignores or casts, and
referenced `EmbeddingError` through `memory_store_embeddings.client`. Runtime
imports that name, but generated `client.pyi` does not export it. The reranker
battery likewise imported `RerankError` from `client` despite its `.pyi`
omission. The repo workaround excludes `**/*_jaunt_tests/` from ty, matching its
existing `**/tests/` policy, while runtime pytest remains required.

Targeted `jaunt test` should select and run generated test-module targets, or
report that zero tests were collected. Test generation should understand the
facade/generated-global boundary and avoid facade monkeypatches that cannot
affect governed functions. JSON output should include the direct pytest command
and result. Generated tests should import exception classes from their defining
`errors` modules and use precise ignores or casts for intentional off-signature
cases. We are refining test intent and the implementation contract rather than
editing generated tests.

### Global `include_target_tests` invalidated modules with no target-test change

Adding this reproducibility setting for the five newly adopted modules had a
workspace-wide fingerprint effect:

```toml
[build]
include_target_tests = true
```

All 12 pre-existing modules (`mcp_memory_server.temporal` plus 11
`memory_store_utils` modules) immediately became stale with reason
`fingerprint`, although their specs and test intent had not changed. Removing
the global key restored those 12 to fresh, but made the five newly built
modules stale again. We therefore had to keep passing
`--include-target-tests` per build.

Config invalidation should be scoped to modules that actually consume targeted
test intent, or each artifact should record its effective include mode without
globally invalidating unrelated modules.

### Generated async-context-manager stubs failed type checking

`uv run ty check` reported three `invalid-argument-type` diagnostics in new
Jaunt 1.7 `.pyi` output:

- postgres `pool.pyi` preserved `@contextlib.asynccontextmanager` on
  `async def get_db_connection` and `get_db_connection_no_tx`, both annotated
  to return `AsyncGenerator`;
- telemetry `tracing.pyi` did the same for `trace_async_span`, annotated to
  return `AsyncIterator`.

In a stub, an ellipsis-body `async def` is treated as a coroutine function, so
it cannot satisfy `asynccontextmanager`'s `Callable[..., AsyncIterator]` input.
We did not edit the generated stubs. The narrow repo workaround excludes only
these two generated `.pyi` paths from ty input; targeted source typing then
passes.

The `.pyi` emitter should special-case decorated async generators and context
managers: emit the post-decoration callable signature, or use a non-async
generator-function stub shape accepted by the decorator. Regression coverage
should run the emitted stub through ty, mypy, and pyright.

### Successful generated artifacts failed the consumer's Ruff gates

The rebuilt behavior passed its existing tests (`memory-store-utils`: 527;
`temporal`: 38), and `ty` passed. Ruff still rejected Jaunt-owned outputs:

- generated `compression_utils.py` and `lexical_match.py` contained duplicate
  typing imports (`F811`);
- generated `cb32.pyi`, `chunking.pyi`, and `compression_utils.pyi` contained
  unused source-only imports (`F401`);
- `ruff format --check` said five generated implementations would be
  reformatted.

We did not hand-edit the generated files. To keep the repo gates green, the
consumer needed narrow exceptions: ignore `F811` only for
`__generated__/*.py`, ignore `F401` only for `*.pyi`, and exclude
`__generated__` from Ruff formatting. A build can therefore succeed, pass its
behavior and type checks, and still emit artifacts that fail the consumer's
lint and format gates.

The same `status`/`build` pass rewrote `treedocs.yaml`'s `project.name` from
the canonical `mem-mcp-b` to the worktree basename
`mem-mcp-c-jaunt-packages`, along with unrelated tree-entry refreshes. We
discarded that churn. A worktree path should not change the committed project
identity or refresh unrelated repo-map entries during a status/build run.

### Campaign cost lower bound was $56.27

Across completed model-backed builds for the five newly governed modules, the
build summaries reported 20 completed API calls and $18.214878 in estimated
cost. That excludes the interrupted duplicate multi-target run and all four
`@jaunt.test` generation costs: the merged-workspace `jaunt test --json`
wrapper returned only per-owner `{}` values, with no usage or cost data.

Combined with the $38.05708 dependency-only refresh above, this session exposed
at least $56.27 in reported generation spend. Much of the new-module repetition
came from first-build contract review plus tool-induced test-root and
fingerprint churn. This is a lower bound for the adoption campaign, not a
steady-state per-module estimate.

Workspace JSON should aggregate build and test-generation costs across owners,
including provider usage already incurred by interrupted work.

## 2026-07-03 — mem-mcp-b PR 1 (first adoption campaign)

Context: jaunt 1.2.0 from PyPI, Codex CLI 0.142.4 (API-key auth), engine
`gpt-5.5@high`, semantic gate on. Pilot conversions: `timing.py`,
`json_utils.py` in a uv-workspace package (`memory-store-utils`). Findings
ordered by severity.

### 1. Generated code ships a silent-fallback ladder (severity: high)

The generated `timing` module wraps its handwritten-symbol imports in
`try/except ImportError` and, on failure, swaps in `_Fallback*` classes:

```python
try:
    from timing import MOCK_TIMING_CALLS as MOCK_TIMING_CALLS
except ImportError:
    _SOURCE_SYMBOL_IMPORT_FAILED = True

class _FallbackMockTimer:
    def stop(self) -> float:
        return float(self.duration_ms)   # no "Timer was not started" guard
```

The fallback *diverges from the spec contract* (no unstarted-`stop()`
ValueError). If the import path ever breaks, behavior changes silently
instead of failing loud. Whether this is model defensiveness or prompt
scaffolding, generation should be instructed (and ideally validated) to
fail loud on import failure — a spec-driven system's generated body should
never contain a second, divergent implementation of the same contract.
Suggest: add an explicit rule to `build_module.md` ("no fallback
implementations; import failures must raise") and/or a validation pass that
rejects `except ImportError` around source-module symbol imports.

### 2. Unknown config sections are silently ignored (severity: high)

We wrote `[gate] model = "gpt-5.4-mini"` (wrong name); jaunt read nothing,
kept defaults, and said nothing. Ground truth is `[semantic_gate]`
(`enabled` / `model` / `reasoning_effort`), found only by reading
`config.py`. A typo'd section or key should at minimum warn, ideally error
(`exit 2`), like the existing missing-source-root check does. Unknown-key
rejection over the whole TOML would have caught this instantly.

### 3. `@jaunt.magic` breaks type-checkers at every call site (severity: medium-high)

With whole-class magic, Pyright reports `Expected 0 positional arguments`
for `Timer(name)` at consumer call sites, and stub functions decorated
`@jaunt.magic()` resolve to `_Wrapped[...]`, which then fails
`reportGeneralTypeIssues` when the stub's name is used in a type position
(`Union[Timer, "MockTimer"]`). Consumers of converted modules inherit this
noise everywhere. The decorators need to be signature-preserving to the
type system: `ParamSpec`-based overloads so `magic()(cls) -> type[cls]` and
`magic()(fn) -> same-signature fn`, or a `TYPE_CHECKING` branch where the
decorator is identity.

### 4. Per-build cost ~2–3× the working estimate (severity: medium)

One trivial module (`timing`, ~100 LOC original, 6 specs) cost **$4.53**:
5 API calls, 2,039,513 prompt + 56,715 completion tokens (1,788,672 prompt
tokens cached), 2 build attempts. Our planning number was $1–2/module. The
prompt context is enormous for a leaf module with zero deps — worth
auditing what lands in `deps_generated_block` / `package_context_block` /
`blueprint_source_block` for small modules, and considering context
budgets scaled to spec size.

### 5. `jaunt instructions` prints no config schema pre-init (severity: medium)

Before a `jaunt.toml` exists, `jaunt instructions` says "No jaunt.toml
found — run jaunt init" and exits without printing the config schema. But
writing `jaunt.toml` is exactly the moment you need the schema (see
finding 2 — we guessed and lost). Print the full annotated schema (or a
commented template) in the no-config case.

### 6. `source_roots` granularity silently changes module identity (severity: medium)

We pointed a source root *inside* the package
(`.../src/memory_store_utils`), so jaunt named the module `timing` and the
generated code does top-level `from timing import ...`. If the root had
been `.../src`, it would presumably be `memory_store_utils.timing`. Two
consequences: (a) generated files aren't importable as ordinary modules
outside jaunt's loader; (b) nothing warned us that our root choice produced
top-level module names that collide with the stdlib/pypi namespace
(`timing` is a real PyPI package). Docs guidance on choosing roots
(package parent vs package dir), plus a warning when a derived module name
shadows an installed distribution, would prevent quiet weirdness.

### 7. Undeclared cross-module deps fail silently into reimplementation (severity: medium)

By design (`build_module.md`), generation may import only: handwritten
symbols of the spec module, declared/inferred Dependency APIs, and nothing
else — "do not guess or fabricate module paths." Right rule, but the
failure mode when a contract *implies* a helper living in an undeclared
user module is silent: the model can't import it, so it reimplements the
logic inline. That shows up only as duplicated logic in the generated
diff, if the reviewer notices. Suggest: instruct the model to emit a
loud marker (comment or build warning) when it needs behavior it cannot
import — "needed `X` from `<module>`, not in Dependency APIs, inlined a
copy" — so the fix (declare the dep) is discoverable instead of archaeological.

### 8. Generated module re-imports spec symbols it just implemented (severity: medium)

The generated `timing` module defines `class MockTimer` (the real
implementation), then later does `from timing import MockTimer as
MockTimer` in the "reuse handwritten symbols" block — rebinding the name
to the spec module's *wrapped stub*. `MockTimer` is a spec, not a
handwritten symbol; the reuse block should never list spec symbols. At
best this is a confusing no-op under the runtime's lazy forwarding; at
worst it's a circular-forwarding hazard. Looks like either the
module-contract classifier includes magic symbols or the model
over-applied the reuse rule — worth a validation check that the generated
module does not import its own `spec_refs` back from the spec module.
(Related positive: lazy forwarding — `importlib.import_module(generated)`
at first call — is what makes spec↔generated mutual imports survivable at
all. Good design; the above just abuses it.)

### 9. Generated-dir layout surprise: `<module>/__generated__/` (severity: low)

For module `timing.py`, output landed in `timing/__generated__/__init__.py`
— a directory sibling that shadows the module name. Expected from the docs:
`<package>/__generated__/<module>.py` (the quickstart shows
`src/my_app/__generated__/specs.py`). Both work under the runtime loader,
but docs and reality should match; the dir-shadowing form also confuses
humans and tools that resolve `memory_store_utils.timing` by path.

### 10. Docs site nits (severity: low)

- `creatorrr.github.io/jaunt` 301s to `jaunt.ing` — fine, but the redirect
  drops deep links.
- `/docs/configuration/` 404s while the codex-engine reference page says
  "consult the Configuration reference" — the page either moved or doesn't
  exist yet (relates to finding 5).

### Addendum — after the first pilot landed (same day)

**11. `jaunt check` does not gate `@jaunt.magic` drift at all (severity: HIGH — highest of the campaign).**
`jaunt check` verifies `@jaunt.contract` batteries only. With zero contract
functions it prints "Contract check: 0 contract function(s)." and exits 0
*regardless of magic-mode freshness*. Magic spec↔generated drift is
tracked only by `jaunt status`. Every piece of adopter-facing framing —
"check is the deterministic CI gate", "exit 4 = stale drift" — led us to
wire `jaunt check` as the required CI check for a magic-mode campaign,
where it is a no-op that always passes. Either `check` should include
magic freshness (or grow `--magic` / an exit-code contract shared with
`status`), or the docs need to say loudly that magic-mode CI gating means
`jaunt status` with exit-code semantics. (We are switching our CI job to
gate on status-based freshness.)

**12. src-layout mapping evidence for finding 6.**
`spec_module_to_generated_module`: bare `timing` →
`timing/__generated__/__init__.py` (the dir-shadowing weirdness in
finding 9), but `memory_store_utils.timing` →
`memory_store_utils/__generated__/timing.py` (correct, matches runtime
import). So for src-layout packages the source root MUST be the package
*parent* (`.../src`), and the wrong choice manifests only at first build —
config validation can't see it, and `status`/`check` on a spec-less repo
pass. A doctor-style check ("this root points at a package; module names
will be bare") would catch it at init time.

**13. Coverage tooling recipe belongs in the docs (severity: low).**
Spec stub bodies are unreachable by design (runtime forwards to
`__generated__`), so any `--cov-fail-under` gate takes a hit per converted
module. The working recipe: add the stub-raise line (e.g.
`raise RuntimeError("spec stub`) to coverage's `exclude_lines`. Adopters
with coverage gates will all need this; one paragraph in the adoption docs
saves each of them the debugging session.

**14. Repo-map coupling restales unrelated siblings — and poisons status-based CI gating (severity: high, compounds finding 11).**
Adding the `json_utils` spec restaled the already-committed, spec-unchanged
`timing` module ("structural", via repo-map coupling). Consequences: (a) a
plain `jaunt build` would have re-invoked the LLM on a module whose spec
didn't change — cost + possible byte churn in committed generated code —
so our agent had to scope with `--target`; (b) `timing` now sits
permanently "stale (structural)" in `jaunt status` despite being correct
and green. This directly undermines the natural fix for finding 11: if CI
gates on status freshness, repo-map cross-staleness makes every
new-spec PR fail on its untouched siblings. Magic-mode CI gating needs a
staleness signal that is (i) deterministic and (ii) scoped to
actually-affected modules — e.g. spec-digest comparison only, with
repo-map restaling downgraded to informational.

**15. Self-import hazard needs a documented pattern (severity: low-medium).**
Same-module sibling calls in generated code must be by bare name, never a
module-level re-import of the spec module (which is mid-import at load
time). We got there with `prompt=` hints on the stubs; it worked, but
every adopter will rediscover this. Either bake the rule into
`build_module.md` unconditionally or document the `prompt=` idiom.

**16. Build noise on workspace-internal deps (severity: low).**
Skill generation attempts PyPI lookups for uv-workspace-internal packages
(404s) and warns "Missing required heading" for several dep skills. Exit 0,
harmless, but noisy enough to obscure real warnings.

**17. Semantic gate: works as advertised (positive).**
A structural-only spec edit was re-frozen by the gate with no codegen call
("Built 0 module(s), skipped 1"), then reported Fresh. Cheap, correct, and
exactly the promised behavior — this is the feature that makes docstring
polishing safe.

### What worked well (so it doesn't get lost)

- **Provenance headers** in generated files (per-spec digests, generation
  fingerprint, tool version) — exactly what makes a deterministic,
  API-key-free `jaunt check` in CI credible.
- **Generated `AGENTS.md`/`CLAUDE.md` inside `__generated__/`** telling
  agents to keep out — nice defense-in-depth with `jaunt guard`.
- **Freshness/`status`/`check` UX** — clean exit codes, `status` names the
  stale module, baseline on an unconverted repo is a clean 0.
- **The daemon's `.jaunt/`-must-be-gitignored guard** — caught a real
  mistake (we almost committed `.jaunt/`).
- **Contract fidelity of the non-fallback generated code** — exact error
  message strings, truncation semantics, monotonic-read behavior all
  honored from the docstring contract on attempt 2.
- **The import model is explicitly channeled, not vibes** — the build
  prompt enumerates exactly what may be imported (same-module handwritten
  symbols with an AST-derived contract block, declared deps as
  `<module>:<qualname>` APIs, package context as anti-hallucination
  grounding) and forbids fabricated paths. Findings 1/7/8 are edge cases
  *of* that design, not arguments against it.

## 2026-07-04 — 1.3.0 upgrade report (same campaign)

Upgraded mem-mcp-b same-day. Verification of the 1.3 fixes, live:

- **Finding 11 fixed and verified**: `jaunt check` exits 4 on a mutated
  spec, 0 after restore. CI gate is now one line; our status-JSON
  workaround is deleted.
- **Finding 1 fixed and verified**: regenerated `timing` has no fallback
  ladder — zero `except ImportError`, zero `_Fallback*`.
- **Finding 14 fixed**: `clean && build` regenerated both modules with no
  sibling-restaling cascade.
- **Findings 2/5/6/12 fixed and immediately useful**: the package-dir
  source-root warning fired on our `apps/memory-api/mcp_memory_server`
  root on first run — the exact latent bug our pilot review had predicted
  for the next conversion wave.

New findings:

**18. `.pyi` emitter places `from __future__ import annotations` mid-file
(severity: medium-high; patched locally, needs 1.3.1).** The stub emitter
harvests imports from the generated module by referenced name, and the
future import rides along into the prelude after other imports. ruff F404;
ty rejects the file outright (`invalid-syntax`). Future imports are
meaningless in stubs — never emit them. Local patch (filter in
`stub_emitter.py` import collection, jaunt's 43 stub tests pass) is in the
checkout at src/jaunt/stub_emitter.py; we hand-dropped the line from our
two emitted stubs once, pending release. Freshness note: `check` stayed
green after the hand-edit, so stub freshness appears inputs-digest-based —
good (tolerates lint autofixes), but worth confirming that's intentional.

**19. Requested context numbers (finding 4 follow-up): `skills_workspace`
is the cost.** Per-module `context_stats` from the 1.3 rebuild:
json_utils 201k chars (~50k tok) — skills_workspace 95%, repo_map 3%;
timing 205k chars (~51k tok) — skills_workspace 93%. The workspace-skills
block is ~19 of every 20 context tokens for a leaf module with zero deps.
Rebuild of both: $6.22, 9 calls, 2.80M prompt tokens (2.48M cached).
A skills budget (or relevance filter) for small modules looks like the
single biggest cost lever for 1.4.

**20. Skillgen can emit double YAML frontmatter (severity: low).** One
generated skill (`hdbscan`) shipped two consecutive frontmatter blocks —
first with `x-jaunt-dist`/`x-jaunt-version`, second repeating
name/description. Last-block-wins parsers drop the jaunt metadata. 1 of
~25 skills affected, so likely a race or a template branch, not systemic.
We merged the blocks by hand.

---

## 2026-07-04 — findings from the PR 2 wave (mem-mcp)

**21. `codex.fingerprint_cli_version` default breaks the deterministic CI
gate (severity: high; the exact failure 1.3 was supposed to prevent).**
The flag defaults to `true`, so `generation_fingerprint` embeds the local
`codex --version` output in every committed header. Any CI runner without
a codex binary resolves it to `"unknown"`, the fingerprint diverges, and
`jaunt check` exits 4 with both modules `stale (structural)` — on a tree
that is byte-identical to the one that built green locally. Bit us on the
first CI run after the 1.3 upgrade; took a clean-room clone + PATH-shadowed
codex stub to isolate, because `check` is honest about *that* environment,
not about the committed tree. Two asks: (a) default it to `false` — the
model + reasoning_effort + sandbox are already runtime_parts, and the CLI
patch version is a cache-partitioning concern, not a drift concern; (b)
whatever the default, `jaunt check` should either exclude
environment-resolved parts from freshness comparison or print which
fingerprint *part* mismatched (we had to read `generate/fingerprint.py` to
find it). Workaround shipped: `fingerprint_cli_version = false` in
jaunt.toml.

**22. No per-module channel for shared constraints → N× duplicated
`prompt=` blocks (severity: medium; authoring smell).** Our pilot
timing.py carried the same ~60-word circular-import warning pasted into
six `@jaunt.magic(prompt=...)` decorators (json_utils had a seventh copy)
because under 1.2 nothing else enforced "generated module must not
re-import spec symbols". 1.3's validator now rejects exactly that
(`_validate_build_contract_only` re-import checks), so we deleted all
seven blocks — but the general gap stands: guidance that applies to a
whole module has nowhere to live except (a) repo-wide
`[build].instructions` or (b) per-decorator `prompt=`. A module-level
channel (module docstring section, or a `jaunt.module(prompt=...)`
directive) would have avoided the duplication and kept decorator noise
down. Related polish: the validator's redefinition error says "Import or
reuse {name} from {spec_module} instead" — for whole-class specs that
advice can reintroduce the decorator-time circular import the other
validator forbids; suggest the message point at call-time/lazy access
instead.

---

## 2026-07-04 (later) — 1.3.1 verified in anger; wave-2 numbers

Upgraded mid-wave (1.3.0 → 1.3.1) right after converting five more modules
(formatting, chunking, compression_utils, deixis, mmr — memory-store-utils
is now fully converted, 7/7). Verification against the release notes:

- **Finding 21 fix confirmed**: default `fingerprint_cli_version = false`
  produces fingerprints identical to our explicit-false workaround —
  deleted the workaround line, headers stayed fresh, CI gate green.
- **Finding 18 fix confirmed**: no local patch needed; local checkout
  restored to upstream. (Under 1.3.0 we were hand-dropping the future
  import from up to 5 stubs per build — glad this one's dead.)
- **Upgrade cost: zero restales.** All 7 modules stayed fresh across the
  1.3.0→1.3.1 bump. Patch upgrades not invalidating built modules is
  exactly the right behavior — worth stating as a compatibility promise.
- **`@jaunt.sig` adopted** in the pilot's two whole-class specs. Note: the
  rename restales the module (decorator identity is structural), so alias
  migration costs one rebuild per module — fine for us, but maybe worth a
  release-note warning since the alias is advertised as "still works".

Wave-2 numbers for the 1.4 context-budget work:
- 5-module batch build: $8.27, 3.70M prompt tokens (3.30M cached), 9 calls.
  Batch amortization works — vs $5.08 for a single-module rebuild the same
  day. skills_workspace still 92–95% of every module's context.
- One real contract bug caught by characterization tests, post-validation:
  generated mmr treated negative cosine similarity as a diversity *bonus*
  (raw `max(sims)`) where the human code floors the penalty at 0. The spec
  docstring hadn't stated the floor; tests caught it, docstring fix +
  rebuild resolved it. Data point for "validation can't check semantics —
  keep characterization tests in the acceptance gate."
- Double frontmatter (finding 20) recurred: `opentelemetry-api` skill, so
  2 of ~27 generated skills now — less "one-off race" than finding 20
  assumed. Merged by hand again.

---

## 2026-07-04 (evening) — 1.4.0/1.4.1 magic_module adoption report

Migrated all of memory-store-utils to module style same-day: 6 of 7 modules
now run `jaunt.magic_module(__name__)` + bare stubs; timing.py reverted to
decorator style (finding 23). Upgrades 1.3.1→1.4.0→1.4.1 both cost **zero
restales** — that's four releases honoring the compat promise now.

### Corrections to my earlier reports

- **`@jaunt.sig` alias migration does NOT cost a rebuild.** My wave-2 report
  said it "costs one rebuild per module" — wrong. `status` shows stale
  (structural) but `build` resolves it via the re-stamp path, free. 1.3.1's
  release-note framing was accurate; retract that ask.
- **The re-stamp path writes an empty `tool_version=`.** Our committed
  `__generated__/timing.py` carries `# jaunt:tool_version=` (blank) from the
  1.3.1 re-stamp. Cosmetic, but it erases provenance the header exists to
  provide, and it makes "which tool built this" archaeology impossible later.

### 23. `importlib.reload()` breaks magic_module modules (severity: high)

`reload(mod)` re-executes the module body, which re-calls
`jaunt.magic_module(__name__)`, which raises
`JauntError: magic_module() was already called for module '...'`. Reload is
a standard test idiom for modules with env-derived module-level state (our
`test_get_timer_respects_mock_flag` does `monkeypatch.setenv(...)` +
`reload`). Decorator mode survives reload fine and always has. Not fixed in
1.4.1 (not claimed to be). We reverted timing.py to decorator style — the
escape hatch's third trigger after "decorated symbol" and "import-time
consumption": *reload-dependent modules*. Suggested fix: when the governing
call arrives for an already-registered module with the same `source_file`,
treat it as a reload and re-register (replace) instead of raising.

### 24. Type checkers reject `...` stub bodies on annotated specs (severity: medium)

The REPLY's example and `jaunt init` scaffold `...` bodies, but ty (and
Pyright) flag a `...`-bodied function with a concrete return annotation:
`invalid-return-type` (implicit `None` return vs `-> Tuple[str, bool]`) plus
"Only functions in stub files ... are permitted to have empty bodies". Our
`poe typecheck` gate failed on 9 diagnostics across the migrated modules.
`raise NotImplementedError` bodies avoid both (a raise never returns), are a
recognized module-mode stub form, and are digest-identical to `...` (both
normalize to empty) — we switched all specs to that form, zero restale.
Suggest docs/init lead with `raise NotImplementedError` for any spec with a
non-`None`/non-`Any` return annotation.

### 25. Decorator→module migration is a paid rebuild in practice (severity: low; expectation-setting)

The REPLY correctly warned `raise RuntimeError("spec stub")` bodies restale
once — but the restale is a **full rebuild**, not a gate refreeze, because
the old body was never stub-normalized so the per-spec structural digest
moves. Cost for us: $0.56 (formatting pilot) + $16.63 (6-module batch, 18
calls, 7.65M tokens — retries included). All regenerated bodies came back
semantically equivalent; tests unchanged and green. Contrast: an
import-reorder-only edit to mmr.py was **refrozen at $0** — the gate's cheap
path works exactly when spec digests are unchanged. If more 1.2/1.3-era
adopters exist, a `jaunt migrate` that rewrites legacy stub bodies and
re-stamps headers (bodies are digest-equal after normalization) would make
the conversion actually free, matching how the REPLY reads.

### 26. Module-scan governance is opt-out by shape — no "newly governed" warning (severity: medium; design)

In decorator mode, governance was explicit opt-in. Under `magic_module`, an
undecorated docstring-only class is silently governed — we only dodged this
because our handwritten `SummaryGenerationError` happens to have a real
`__init__`; a bare `class FooError(RuntimeError): """..."""` added to a
governed module later becomes a codex-generated spec with no signal beyond
a new `__generated__` symbol in the build diff. The scan already warns on
import-time *consumption*; suggest a parallel warning (build/check/specs)
when a scan governs a symbol that has no prior generated body — that's the
exact moment accidental governance is cheap to catch.

### Numbers for the finding-19 file

- formatting pilot rebuild: $0.56, 264k prompt (234k cached), 1 call.
  `skills_workspace` 219,124 chars / ~54,781 est tokens of a ~57,721-token
  context — still ~95% of everything the model reads.
- 6-module batch: $16.63, 18 calls, 7.65M tokens. Bigger than wave 2's
  $8.27/5-module because compression_utils + mmr are the two largest specs
  and retries landed there.
- 1.4.x stub emitter: output is unformatted (double blank lines,
  single-quoted `__all__`, `...` on its own line) and still carries the
  unused `import jaunt` (ruff F401) and the dropped-guarded-import string
  annotation (F821 on `"RecursiveChunker | None"`); our scoped per-file
  lint exemptions from the 1.3.1 wave remain in place. Fold into finding 20's
  emitter-hygiene bucket.

---

## 2026-07-04 (night) — 1.4.2 verified; memory-store-utils is 7/7 module-style

- **Finding 23 fix confirmed**: un-reverted timing.py to module style; the
  `monkeypatch.setenv` + `reload` test passes. The conversion was fully
  digest-neutral this time (stub bodies were already `raise
  NotImplementedError`) — build cost $0, exactly the free path the 1.4.0
  notes promised. The escape-hatch trigger list is back down to two
  (decorated symbol, import-time consumption).
- **Emitter fixes confirmed, exemptions deleted**: no more `import jaunt` in
  stubs; the optional-dep string annotation resolves via `RecursiveChunker =
  Any`; output is ruff-formatted. We removed both the ruff F401/F821
  per-file-ignores and the ty `unresolved-reference` override from the 1.3.1
  wave. The no-rewrite-when-fresh hardening also holds: ruff autofix touched
  a committed stub and `jaunt check` stayed fresh — the 1.3.1-era
  ruff-vs-emitter fight loop is dead.
- **Stub-format migration note was accurate**: `check` exited 4 post-upgrade;
  one model-free `jaunt build` re-emitted 7 stubs; committed.
- **One residual emitter nit (low)**: the `X = Any` optional-dependency
  fallback is emitted *above* the remaining imports, so E402 fires on every
  import that follows (3 in chunking.pyi). We re-added a narrow
  E402-only per-file-ignore. Suggest emitting the import block first, then
  fallback assignments.
- **tool_version fix confirmed**: re-emitted headers all carry
  `tool_version=1.4.2`; no blank fields.

---

## 2026-07-05 — 1.5.0 verified; the exemption count hits zero

- **Zero restales on upgrade** (fifth consecutive release). Orphan gate:
  clean here (we've never deleted a spec), so no `clean --orphans` needed;
  the "only blocks if you already have orphans" caveat framing was accurate.
- **E402 fix confirmed the low-friction way**: deleted `chunking.pyi`, ran a
  model-free build, re-emitted stub has `X = Any` after the import block.
  Deleted the E402 per-file-ignore — **we now carry zero jaunt-related lint
  or type-checker exemptions**, down from a peak of a local source patch +
  fingerprint workaround + three scoped exemptions in the 1.3.0 era.
- **Finding 19 resolution — accepted, and a correction on our side**: our
  "skills_workspace is ~95% of every prompt" line treated seeded-on-disk
  bytes as consumption; the lazy-load probe (3 of 13 SKILL.md bodies opened)
  settles it. Glad the answer was instrumentation rather than pruning
  machinery. The honest rename (`skills_workspace_seeded`) is the right fix.
- **Finding 25 (`jaunt migrate`) — moot for us** (we paid the rebuild before
  it existed) but plan-by-default + dirty-tree refusal + the
  `--allow-newly-governed` guard is exactly the shape we asked for. The
  format-version stub re-emit folded in kills the "run build once" dance —
  good.
- **Finding 26 + orphan lifecycle — adopted into our docs** (AGENTS.md and
  the adoption guide now teach `clean --orphans` and the newly-governed
  flag). The pre-spend placement of the newly-governed warning is the part
  that matters; that was the whole hazard.
- **Advisories**: none emitted on our (model-free) 1.5.0 builds yet; we'll
  report the first real ones from the temporal.py conversion (PR 3b), which
  is the most ambiguity-prone contract in the campaign — a good first test.

---

## 2026-07-04 (PR 3b attempt) — temporal.py conversion blocked; findings 27–28

First conversion outside the utils package (`mcp_memory_server.temporal`,
apps/memory-api source root — date parsing + Pacific-display formatting,
the campaign's densest contract). The generation itself eventually
converged and passed validation; a path-routing bug then put the artifact
where Python can't import it. Spend: $22.29 across 3 builds (9 failed
attempts + 1 success). We reverted to the human implementation; the
characterization suite (33 tests, committed first per the hardening
policy) is what caught both problems. Artifacts preserved locally for a
free-ish resume: spec, .pyi, generated body + contract sidecar.

### 27. No sanctioned third-party import channel in the build prompt (severity: high; cost multiplier)

`build_module.md` says "Only import dependencies listed above — do not
guess or fabricate module paths", where "above" is the Dependency APIs
block (spec-registry modules only). There is no rule for installed
third-party distributions. Our spec's public signatures use
`whenever.Instant` — declared in the app's pyproject, skill seeded, and
imported by the spec module itself — and gpt-5.5 refused to write
`from whenever import Instant` across NINE attempts / $9.80: it copied the
annotations, then contorted (string annotations, `# noqa`, duck-typed
`py_datetime()` shims, delegation stubs), failing ty's
`unresolved-reference` every time. An explicit per-module
`magic_module(prompt="whenever is an installed dependency; import it")`
did NOT override the Rules section — the round-2 advisory says so
verbatim: "whenever is not an allowed declared import in this generation
context". (numpy in our mmr build worked only because that attempt ignored
the rule — model-boldness variance, not policy.) The retry loop cannot
escape this class of failure because the root cause is prompt policy, not
model error; ty output fed back N times just produces N contortions.
**Ask**: an explicit rule — stdlib and installed third-party distributions
that the *spec module itself imports* (or that the owning package declares)
are importable from their real modules; keep JAUNT-NEEDS-DEP for
everything else. Workaround that converged for us ($12.49): instruct
`from __future__ import annotations` + call-time
`importlib.import_module('<spec_module>').Instant` — the handwritten-reuse
idiom — but that's contortion nobody should need for a declared dep.

### 28. Multi-root repos: generated bodies are routed to the FIRST source root (severity: critical for multi-root; blocks PR 3b)

`cli.py:2159` (and the same pattern at ~2684, ~2701, ~3504):
`package_dir = next((d for d in source_dirs if d.exists()), None)` — one
package_dir for every module, the first existing source root. With
`source_roots = ["packages/python/memory-store-utils/src",
"apps/memory-api"]`, the generated body for `mcp_memory_server.temporal`
was written to
`packages/python/memory-store-utils/src/mcp_memory_server/__generated__/`
— a bogus package grafted into the *other* workspace member (it would
ship in the utils wheel if committed). Runtime resolves
`mcp_memory_server` to the real package under apps/memory-api, finds no
generated module, and every call raises JauntNotBuiltError. Worse:
`status`/`check` read through the same wrong path, so the tree is
**fresh-and-green while runtime is broken** — CI's `jaunt check` cannot
catch it; only our characterization tests did. The `.pyi` stub landed
correctly next to the spec, which shows the right pattern: resolve
per-module from the spec's own `source_file` (the root that contains it),
not per-project. ~110 `package_dir` uses across builder/status/check/
orphans/migrate share the assumption — we didn't attempt a local patch.
This is the actual blocker for our memory-api wave; everything before it
(discovery, prescreen, validation, generation) handled the second root
fine.

### Advisories: verdict after first real exercise — keep them, they paid rent immediately

- Round 2's advisory stated the model's own reasoning ("whenever is not an
  allowed declared import...") — that one line ended an hour of guessing
  and sent us to the prompt template. Exactly the observability findings
  27 needed.
- Round 1's advisory revealed sibling spec contracts are not in a
  symbol's generation context (our `_coerce_utc_datetime` docstring
  cross-referenced `parse_temporal_reference` step 1; the generator said
  it wasn't visible). Worth either including same-module sibling
  docstrings in context or documenting "inline shared rules in the
  magic_module prompt" as the pattern — we did the latter and it worked.
- The success-run advisory flagged genuine contract noise: our "no `may`
  abbreviation" rule is unobservable (identical token to the full name).
  A generator that reviews the spec back at you is a feature; consider
  surfacing advisories in `jaunt jobs`/PR-comment form for daemon runs.

---

## 2026-07-05 — PR 3 landed: temporal.py converted (mem-mcp-b); finding 28 workaround, finding 29

Fresh conversion in the mem-mcp-b checkout (not a resume of the blocked
attempt above): 16 module-style stubs, constants handwritten, converged and
**shipped** — characterization suite (now 38 tests incl. parsing/display
files) passed unchanged, full unit suite showed zero regressions vs a
clean-tree baseline, ruff/ty/check green. Spend: $53.69 over 3 builds
($12.32 fail, $21.24 fail, $20.14 success — 2 attempts). Context: 259k
chars (~64k tok), `skills(seeded)` 91%.

### Finding 28 update — the workaround that works: one jaunt project per adopted package

No local patch; adopter-side fix verified end-to-end. Give each adopted
package its own `jaunt.toml` (`source_roots = ["."]` at the package's
sys.path root) and run jaunt from that directory. Everything that resolves
against `source_roots[0]`/config root then resolves correctly: output
placement, `check`, the ty sandbox, pyproject discovery (see finding 29).
CI runs `jaunt check` once per project dir. Residuals worth knowing:

- `[codex]` and `[build].instructions` must stay **byte-identical** across
  the configs — both feed the generation fingerprint, so drift restales
  (re-bills) every module in that project. Split configs turn "guidance
  lives once" into "guidance lives once per project"; a config `include` or
  shared-fragment mechanism would remove the footgun.
- `treedocs.yaml` splits per project (541 entries migrated from the root
  index to the new project's on first `jaunt tree`). Coherent, but
  surprising if you expected one repo index.
- Verified the split is fingerprint-neutral: the freshly built module and
  all 7 utils modules stayed fresh across the config split, $0.

Ask unchanged: resolve per-module from the spec's own `source_file`. Interim
ask sharpened: until that lands, `len(source_roots) > 1` should be a hard
config error (exit 2) — 1.5.0's silent half-working multi-root is the
fresh-and-green-while-runtime-is-broken trap from finding 28, and the config
schema actively invites it.

### 29. Undeclared-import validator resolves deps from the config-root pyproject (severity: high; second layer of finding 27)

`validation.py` `_validate_generated_import_provenance` →
`_declared_project_dependencies(_find_pyproject(project_dir))`:
`project_dir` is the jaunt project root, and `_find_pyproject` walks *up*
from there. In a uv workspace with the config at repo root, that finds the
workspace-root pyproject (ours declares only `openai`) — never the owning
package's pyproject where the dep actually lives. Net effect: **every**
third-party import in generated code is rejected as undeclared, however
correctly declared the package is. This is the second layer under finding
27's nine-attempt loop: round 1 here failed on prompt policy (model
refused the import), round 2 failed on this validator (the advisory quoted
it verbatim: "importing `whenever.Instant` is also rejected as undeclared
by the provided previous-attempt errors"), and mmr's numpy import passed
under 1.4.0 only because this validation didn't exist yet — under 1.5.0 it
would be rejected too. Escape hatches, both verified: (a)
`build.generated_import_allowlist = ["whenever"]` — the error message
advertises it, it works, and the message is the only place it's
documented; (b) per-package projects (finding 28 workaround), which make
`_find_pyproject` land on the right file so declared deps resolve
naturally. Ask: resolve declared deps from the pyproject that *owns the
spec's source root* (walk up from the spec file, not the project dir) —
same per-module resolution principle as finding 28.

### Finding 27 partial confirmation — with the validator unblocked, prompt guidance lands

Once `whenever` was allowlisted, a `magic_module(prompt="import Instant
directly at module scope; no duck-typed stand-ins; no dynamic imports")`
converged in 2 attempts. The final module imports `from whenever import
Instant` at top level like any human-written file. So the finding-27 ask
stands for the *default* behavior, but prompt-level guidance does work once
the rejection layer stops contradicting it.

### Contract-silence data point (for the "validation can't check semantics" file)

Generated `parse_temporal_reference` wraps the year/year-range constructors
in `try/ValueError → None`; the human code let `datetime(0, ...)` raise on
degenerate inputs like `"0000"` (`\d{4}` admits year 0). Spec was silent,
tests don't cover it, both behaviors defensible — generation chose the more
defensive one. Harmless here, but it's a clean example of the class:
divergence invisible to every gate, caught only by line-review of the first
build. Mentioning since advisories flagged nothing (correctly — the spec
really was silent).

### Advisories: second real exercise, paid rent again

The round-2 advisory named the exact rejection ("...rejected as undeclared
by the provided previous-attempt errors") — that one line is what sent us
into `validation.py` and turned a mystery retry loop into finding 29 in
about ten minutes. Two-for-two on advisories ending archaeology sessions.

## 2026-07-10 — 1.6.1 upgrade verified; finding 30 (the per-module root resolution request, in full)

Context: mem-mcp-b, 1.5.1 → 1.6.1 in one hop. Both projects (repo-root
`memory-store-utils`, 7 modules; `apps/memory-api` `mcp_memory_server`,
1 module) adopted the new `gpt-5.6-sol@medium` default by editing both
`[codex]` blocks explicitly. Result: free re-stamp in both projects
(`Built 0, skipped 7` / `Built 0, skipped 1`), `check` green, $0. The
1.5.1 fingerprint re-stamp behavior held exactly as advertised — config
churn without regeneration is now genuinely cheap, which we relied on
twice today.

Two small notes before the main event:

- The 1.6.1 default-model change landed in a patch release and (per the
  PR's own bot review) alters the generation fingerprint for
  minimally-configured projects. We were shielded by explicit `[codex]`
  blocks, but the semver contract "patch never restales" is worth
  keeping — adopters plan spend around it.
- `jaunt build` in the memory-api project now warns `31 generated
  skill(s) were missing required section headings: PyYAML, aiosql, ...`.
  These are jaunt's own earlier skill outputs failing jaunt's own newer
  validator. Cosmetic, but a `jaunt skills --regenerate-invalid` (or
  auto-heal on build) would beat a wall of warnings nobody can act on.

### 30. Per-module source-root resolution — the detailed ask (severity: high; supersedes the asks in 27/28/29)

This is the standing request from findings 27–29, written out as a design
request now that 1.6 shipped without it. The one-project-per-package
workaround works (we run it in CI daily) but it converts one design gap
into four adopter-side obligations, and every new package we adopt adds
another copy of each.

**The rule we want:** resolve everything that is currently derived from
`source_roots[0]`/config root *per spec module*, by walking up from the
spec's own `source_file`:

1. **Owning package** = nearest ancestor `pyproject.toml` of the spec
   file. This single lookup should drive all of:
   - module identity (the dotted name — finding 6's granularity trap
     dies with it);
   - `__generated__/` and `.pyi` placement (beside the spec, under the
     owning package's source tree);
   - declared-dependency resolution for the undeclared-import validator
     (finding 29 — walk up from the spec file, not the project dir);
   - the ty sandbox root and pyproject discovery for type-checking;
   - test-root association (nearest `tests/` under the owning package,
     or an explicit per-package mapping in config).
2. **Config collapses back to one file.** With per-module resolution,
   `source_roots` can safely list every adopted package (or a glob:
   `source_roots = ["packages/python/*/src", "apps/memory-api"]`), and
   the 1.5.1 multi-root exit-2 gate becomes unnecessary. One `jaunt.toml`
   means `[codex]` and `[build].instructions` are shared by construction
   — the byte-identical-blocks footgun (finding 28 residual) is not
   mitigated but *deleted*. Today a one-character drift between our two
   configs would silently restale and re-bill 8 modules; we have a
   comment in each file begging future editors not to touch them. That
   is not a stable equilibrium.
3. **Per-package artifacts are fine; per-package *config* is the
   problem.** We don't mind `treedocs.yaml` or journals splitting per
   owning package (1.5's per-project split was coherent). Keep those
   wherever resolution puts them — just don't make us duplicate policy.
4. **Migration must be fingerprint-neutral.** The 1.5.1→1.6.1 re-stamp
   proves the machinery exists: merging N per-package projects into one
   per-module-resolved config should re-stamp, not rebuild. If module
   identity is computed from the owning package (point 1), dotted names
   don't change and digests shouldn't either. A `jaunt migrate
   --merge-projects` that verifies $0 before touching anything would
   make adoption a non-event.
5. **CI collapses to one gate.** `jaunt check` at the repo root, exit 4
   on any stale module in any package. Today we run it once per project
   directory and the workflow file grows a step per adopted package.

### 1.6.1 build-economics data points (same day, same repo)

- **Removal-only spec edits classify as free re-stamps.** Deleting three
  governed stubs from `timing` and two handwritten classes (+`__all__`
  entries) from `compression_utils` left both modules stale as
  `re-stamp: free` — no regeneration billed. Excellent behavior, one
  wrinkle: the re-stamped generated file keeps the removed symbols' dead
  bodies as latent text (unbound at runtime, correct `.pyi`), which then
  shows up as uncovered lines under a coverage floor that counts generated
  code. `jaunt build --force --target <module>` prunes them for one
  module's build price. A removal-only re-stamp could prune deleted
  symbols' bodies textually for $0 — they're identifiable from the spec
  diff.
- **gpt-5.6-sol@medium is ~3× cheaper than gpt-5.5@high here.** Forced
  `timing` rebuild: $1.33 (2 calls, 619k prompt / 82% cached). Four fresh
  small-module conversions (`coercion`, `db_errors`, `entity_text`,
  `lexical_match`): $5.56 total, all four converged, moved-in acceptance
  suites passed unchanged on first run. The finding-4 cost complaint
  ($4.53 for one trivial module) is effectively resolved by the new
  default engine.
- Advisories again earned their keep: the `coercion` build flagged that
  our contract's "never raises" overpromises against "catches only
  JSONDecodeError" — a real spec ambiguity we inherited from the code we
  were porting.

Scale note for prioritization: we're at 2 projects/8 modules and the
overhead is already comment-guarded duplication + N CI steps + "run
jaunt from the right directory" tribal knowledge (our AGENTS.md spends
a paragraph on it, and a session hook reminds every agent). The repo
has ~5 more utility packages that are natural adoption targets; each
one currently costs a new jaunt.toml with hand-synced `[codex]` and
`[build]` blocks. Per-module resolution is the difference between
"adopt a package = add one glob entry" and "adopt a package = add a
config file that can silently re-bill the others if it drifts."

## 2026-07-10 (evening) — 1.6.2 verified: finding 30 closed, consolidation was a non-event

Same repo, same day, 1.6.2 from PyPI. The `migrate --merge-projects`
path worked exactly as the reply promised:

- **Preview** reported `neutral: true`, both configs discovered, all 10
  module routes (by then we'd grown to 10 — 4 fresh conversions landed
  between 1.6.1 and the merge) individually neutral, zero conflicts.
- **Apply** refused twice before succeeding, both refusals correct: a
  dirty tracked tree first, then untracked files. Strict, but the right
  kind of strict for a config rewrite — no complaints. (A
  `--allow-untracked` might be worth it; untracked files can't affect
  neutrality.)
- **Post-merge**: child config deleted, root `[paths]` rewritten with
  the exact explicit roots from the reply, `jaunt status` shows 10/10
  fresh from the root, one `jaunt check` gates everything, `$0` spent.
  First root `jaunt tree` absorbed the child's 690 treedocs entries; we
  deleted the now-orphaned child `treedocs.yaml` by hand (migrate could
  plausibly do that itself).
- CI collapsed from two check steps to one; the byte-identical-blocks
  comment guards are deleted from our configs and AGENTS.md. The
  "adopt a package = add one glob entry" end state is real now.
- Also confirmed: the 1.6.2 semantic-gate default change
  (`gpt-5.6-luna@medium`) restaled nothing, as promised — gate settings
  staying outside the fingerprint is the right call.

Finding 30 is closed from our side. Two-day turnaround from
implementation-level ask to shipped-and-verified is the best adopter
experience we've had with any tool this year.
