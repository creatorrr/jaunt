# Adoption feedback

Running notes from real adopters. Newest section first.

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
