# Adoption feedback

Running notes from real adopters. Newest section first.

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
