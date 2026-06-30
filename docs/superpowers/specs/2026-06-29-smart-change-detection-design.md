# Smart change detection (linter-resistant digests + nano semantic gate) — Design

**Date:** 2026-06-29
**Status:** DRAFT — design approved in brainstorming; pending plan + implementation.
**Engine assumption:** Codex is the sole engine (`codex exec`), per
`docs/superpowers/specs/2026-06-24-codex-engine-design.md`.

> **Codex consult (2026-06-29):** This design was reviewed by `codex exec` at
> `reasoning_effort=high` against the real source. Its findings reshaped it: the
> gate strategy flipped from drift-check to **contract-text diff**; the
> "deterministic dependency short-circuit" was downgraded to **semantic caching**;
> Layer A picked up several correctness requirements (spec name + symbol kind,
> async-ness, class attribute *values*, `@jaunt.preserve` bodies, effective
> decorated signature, non-stub-body handling); a **digest-scheme tag + migration
> escape hatch** was added; and **deterministic validation before any re-freeze**
> became a hard requirement. See §9 for the review trail.

---

## 1. Context & motivation

Freshness in Jaunt is **module-level**. Each generated file
(`__generated__/<mod>.py`) carries a header (`src/jaunt/header.py`) with a
`module_digest` = SHA-256 over each spec's source + decorator kwargs + transitive
dependency digests (`src/jaunt/digest.py`). On `jaunt build`,
`detect_stale_modules` (`builder.py:143`) recomputes and compares; **any** mismatch
marks the whole module stale and hands it to Codex `gpt-5.5` at
`reasoning_effort=high` for full regeneration. "Freeze" = the digest written into
that header by `write_generated_module` (`builder.py:93`). `jaunt test` mirrors this
via `detect_stale_test_modules` → `_test_module_digest` → `module_digest`
(`tester.py`).

Two weaknesses make this needlessly expensive:

1. **The digest hashes raw source.** `local_digest` (`digest.py:97`) hashes the raw
   source segment from `extract_source_segment` (only trailing-whitespace/blank-line
   trimming). So a **ruff reformat** (requoting, re-wrapping a signature, trailing
   commas), a **comment edit**, or a **docstring re-indent** flips the digest →
   false-positive staleness → a full `gpt-5.5` rebuild for a no-op change.
2. **No semantic gate.** The instant the digest differs, the module goes straight to
   the big model. Nothing asks whether the change actually altered the behavioral
   contract.

The fix is two independent layers: a deterministic digest that ignores cosmetic
noise (Layer A), and a cheap `gpt-5.4-nano` gate that judges whether a *real* text
change is behaviorally meaningful before paying for `gpt-5.5` (Layer B).

## 2. Goals / Non-goals

**Goals**
- Ruff reformatting, comment edits, and whitespace/quote/indent changes to specs
  **never** trigger a rebuild (deterministic, no model call, build + test).
- A genuine but **behaviorally-equivalent docstring reword** does not trigger a
  `gpt-5.5` rebuild: a `gpt-5.4-nano@high` gate judges it and the module is
  **re-frozen** (header digests rewritten on the unchanged, validated body) instead.
- Never silently skip a *needed* rebuild (low false-KEEP): fail-safe to REBUILD on
  any ambiguity, error, structural change, or failed validation.
- One-time, model-free migration when the digest scheme changes (no nano storm on
  the first upgraded build).

**Non-goals**
- Changing the module-level granularity of generation. Generation stays whole-module.
- Making `jaunt status` / `jaunt check` call a model. They stay deterministic.
- Sub-module (per-symbol) regeneration. Out of scope.
- Re-freezing files that fail deterministic validation or look hand-edited.

## 3. Design overview — two layers

```
spec source edited
   │
   ▼
Layer A: normalized per-spec contract digest  ──(unchanged)──▶ module FRESH, skip
   │ (changed)
   ▼
classify the change per changed spec:
   structural (sig/kind/decorators/class members) ──▶ MEANINGFUL (no model call)
   prose-only (docstring text)                     ──▶ Layer B nano contract-diff
   │
   ▼
Layer B verdict per spec: EQUIVALENT | MEANINGFUL  (fail-safe → MEANINGFUL)
   │
   ▼
roll up over the dependency graph:
   module has any transitively-relevant MEANINGFUL spec ──▶ REBUILD (gpt-5.5)
   else (stale only via EQUIVALENT changes)             ──▶ validate + RE-FREEZE
```

## 4. Layer A — linter-resistant normalized digest (deterministic, always on)

Replace raw-source hashing in `local_digest` with an **AST-normalized per-spec
contract**. Codex's review is explicit that `contract_digests()` (`digest.py:189`)
is *not* a drop-in — it only handles top-level sync contract functions — so this is a
new, carefully-scoped normalizer that must mirror what the build prompt and validator
actually treat as contract material.

### 4.1 The normalized contract of a spec

A spec's normalized contract is the tuple, each component hashed into a stable blob:

1. **Identity**: the canonical `spec_ref` (`pkg.mod:Qualname`) and **symbol kind**
   (`function` / `async_function` / `method` / `async_method` / `class`). Codex
   caught two real traps here:
   - `module_digest` (`digest.py:146`) hashes *sorted digest values*, not
     `spec_ref → digest`. A rename with an identical signature+docstring could slip
     through if the name isn't inside the per-spec payload. **Include the name.**
   - `ast.unparse(node.args)` does **not** encode `async`. **Include kind explicitly.**
2. **Signature**: the **effective decorated signature** (args incl. defaults,
   `*args`/`**kwargs`, kw-only, annotations, return annotation), normalized via
   `ast.unparse`. `ast.unparse(node.args)` *does* capture defaults/annotations/star
   args (confirmed by Codex), but the build prompt injects an *effective* signature
   and decorator-API records separately (`builder.py:~1230`), so the digest must use
   the same effective signature the generator sees, not the raw AST args alone.
3. **Decorator metadata**: jaunt decorator kwargs (`deps`, `targets`, `prompt`,
   `infer_deps`, …) normalized as today (`_normalize_spec_refs_for_kwargs` +
   `_jsonable`), **plus** any non-jaunt decorators' normalized form (they change the
   public API the generator must produce).
4. **Prose**: the cleaned PEP-257 docstring (`ast.get_docstring(node, clean=True)`).
   This is the only component the nano gate ever diffs.
5. **Body (conditional)**: for a function/method spec, drop the body **only if it is a
   stub** (`raise …` / `...` / `pass`). The runtime does **not** currently reject
   non-stub `@magic` bodies, so if the body is non-stub, hash its `ast.unparse`d,
   docstring-stripped form (so real logic in a body is never silently ignored).
6. **Whole-class members**: mirror `class_analysis.split_class_members()`
   (`class_analysis.py:63`, `builder.py:318`) exactly — class name, bases, keywords,
   class-level decorators, and per member: attribute **annotations and default
   values** (`ast.unparse` of `AnnAssign`/`Assign`, values included — they are
   contract material the validator checks verbatim), method signatures + docstrings.
   **Stub method bodies are dropped; `@jaunt.preserve` / non-stub method bodies are
   kept, normalized**, with the `@jaunt.preserve` decorator stripped the same way the
   validator strips it.

Effect: ruff formatting, comments, blank lines, and quote style never change the
hash. A docstring reword changes only component 4. A signature/kind/decorator/member
change is "structural" (components 1–3, 5–6) and is treated as MEANINGFUL without a
model call.

### 4.2 Where it plugs in

- `local_digest(entry)` returns the SHA over the full normalized contract. Because
  `graph_digest` / `module_digest` (build) and `_test_module_digest` (test) both route
  through `local_digest`, **Layer A benefits build and test for free**.
- We additionally expose, per spec, the **structural sub-digest** (components 1–3,5–6)
  and the **prose sub-digest** (component 4) separately, so the change classifier (§6)
  can tell a structural change from a prose-only one without re-parsing.

## 5. Per-spec metadata: header tag + prior-contract sidecar

To localize changes and to feed the nano diff its "before", we persist two things at
freeze time:

- **Header** (`header.py`): add `# jaunt:spec_digests={"<ref>":{"s":"sha…","p":"sha…"}}`
  (structural + prose sub-digests per spec) and a **scheme tag**
  `# jaunt:digest_scheme=2`. The header stays small (digests only). Backward
  compatible: a file lacking `spec_digests` → treat all its specs as changed.
- **Sidecar** (co-located, atomic with the module write):
  `<generated_module>.contract.json`, mapping `spec_ref → {kind, signature,
  decorator_meta, prose}` — the **prior normalized contract text** the nano gate
  diffs against. Kept out of the header to avoid bloating it with full docstrings.
  Rewritten on every build *and* every re-freeze so it always matches the frozen
  digest. If the sidecar is missing/corrupt for a prose-only change, the gate
  fails safe → MEANINGFUL (rebuild).

> Rationale for the contract-text diff (Codex's recommendation, user-approved):
> comparing old-prose vs new-prose ("same behavioral meaning?") is a narrow,
> reliable task for a small model with the **lowest false-KEEP** rate. The earlier
> drift-check (audit the whole generated module against N contracts) is exactly
> where a nano model misses obligations. Storing the prior contract is cheap because
> we are already extending per-spec metadata.

## 6. Change classification (deterministic) + Layer B gate

For each module flagged stale by `detect_stale_modules`, compute current normalized
contracts and diff against the stored per-spec sub-digests:

1. **Structural change** (structural sub-digest differs, or spec is new, or sidecar
   missing) → classify the spec **MEANINGFUL**. No model call.
2. **Prose-only change** (only the prose sub-digest differs) → **Layer B nano call**:

   > Prompt (read-only `codex exec`, model `gpt-5.4-nano`, `reasoning_effort=high`):
   > "A Python symbol's behavioral contract is its docstring. Signature is
   > unchanged: `<signature>`. OLD docstring: `<old prose>`. NEW docstring:
   > `<new prose>`. Does the NEW docstring demand any behavior the OLD one did not,
   > or forbid/relax anything the OLD one required (different result, error,
   > ordering, edge case, complexity, type)? Reply exactly `EQUIVALENT` or
   > `MEANINGFUL`. If uncertain, reply `MEANINGFUL`."

   Parse for an exact token. `EQUIVALENT` → keep; anything else / error / empty →
   **MEANINGFUL** (fail-safe).
3. **No change** (both sub-digests equal but module digest flagged it) → only
   possible during scheme migration (§8); classify EQUIVALENT.

**Granularity:** the gate runs **per changed prose-only spec** (one narrow nano call
each), not per module and not over generated code. This answers Codex's "wrong
reliability shape" concern: nano is only ever asked the contract-equivalence
question, never asked to audit code or reason about shared helpers. No N-of-M voting
(correlated failure, extra cost); the conservative single-call + fail-safe bias is
the policy.

## 7. Rollup, re-freeze, and dependency propagation

Per-spec verdicts roll up to the existing module/spec dependency graph:

- A module **REBUILDS** iff any spec in its transitive dependency closure (including
  its own specs) is **MEANINGFUL**. This reuses `expand_stale_modules` /
  `detect_api_changed_modules` semantics: a MEANINGFUL spec is an API-changing spec.
- A module is **RE-FROZEN** iff it is stale but every changed spec in its transitive
  closure is **EQUIVALENT**.

**On the dependency case (Codex finding #3):** `module_api_digest` *includes*
docstrings and dependents' prompts embed dependency docstrings
(`build_dependency_api_block`, `module_api.py:112`; `_collect_dependency_context`,
`builder.py:1093`). So a dependency's prose reword *does* change a dependent's
generation surface. We do **not** claim re-freezing the dependent is "deterministic."
Instead: the dependency's own per-spec verdict governs — if the reworded dependency
contract was judged EQUIVALENT, the dependent re-freezes; if MEANINGFUL, the
dependent rebuilds (existing cascade). This is **semantic caching**, consistently
applied (the same judgment that let the dependency re-freeze), not a freshness hack.

**Re-freeze** = `builder.refreeze_module(...)` (+ `tester` mirror): read the existing
file, strip the old header, recompute all current header fields (`module_digest`,
`module_context_digest`, `module_api_digest`, `generation_fingerprint`,
`spec_digests`, `digest_scheme`, `spec_refs`), rewrite the **unchanged body** + new
header atomically (same path as `write_generated_module`), and rewrite the sidecar.

**Validate before re-freeze (hard requirement, Codex #6):** before re-freezing, run
the same deterministic validation the builder uses (AST validation in
`validation.py`; the `ty` check if `build.ty_retry_attempts` applies) against the
existing body. If it fails — broken or hand-edited generated file — **REBUILD**
instead of blessing it. A re-freeze must never certify code that doesn't pass the
gates a fresh build would.

## 8. Configuration, flags, migration

**Config** — new `[semantic_gate]` table (named for what it is; Layer A is also
"change detection", so `--no-change-detection` would be a misnomer per Codex #6):

```toml
[semantic_gate]
enabled = true                # default-on, conservative
model = "gpt-5.4-nano"
reasoning_effort = "high"
```

New `SemanticGateConfig` dataclass + parsing/validation in `config.py`. The nano call
reuses `run_codex_exec` (`generate/codex_backend.py:84`) with `model` /
`reasoning_effort` overrides and `sandbox="read-only"`.

**Flags:**
- `--force` — bypass everything; rebuild all (unchanged behavior).
- `--no-semantic-gate` — keep Layer A (linter-resistance), skip Layer B; every
  normalized-digest change rebuilds. Available on `build` and `test`.

**Migration (scheme bump, Codex #5):** headers gain `digest_scheme`. On the first
build after upgrade, every module looks stale (raw scheme `1` digest ≠ normalized
scheme `2` digest). Escape hatch, applied before any gate call: if a file has no
`digest_scheme` (or `=1`), recompute the **old raw `module_digest`**; if it matches
the on-disk digest **and** `generation_fingerprint` / context / api digests still
match, the file is genuinely fresh under the old scheme → **silently re-freeze** with
scheme-2 digests (validate first), no nano call, no rebuild. Only if the old raw
digest *also* mismatches is the module truly changed and routed through §6. This
prevents a nano storm on upgrade.

**`jaunt status`** stays model-free: it reports digest-stale modules and (using the
header `spec_digests`) can label each as structural vs prose-only changed, but it
never runs the gate. `build --json` / `test --json` gain a `refrozen: [...]` field
alongside `generated` / `skipped` / `failed`.

## 9. Codex review trail (what changed and why)

- **Gate strategy flipped** drift-check → **contract-text diff** (Codex #1): lower
  false-KEEP, narrower task for nano. User-approved.
- **Layer A correctness** (Codex #2, "big problems"): include spec name + symbol
  kind; encode async-ness; use the effective decorated signature + decorator-API
  records; hash class attribute *values*; mirror `split_class_members` for
  preserved/non-stub bodies; only drop stub function bodies, else hash the normalized
  body. The "`contract_digests` already does this" claim was wrong and is removed.
- **Dependency short-circuit** downgraded from "deterministic" to **semantic
  caching** with per-spec verdict propagation (Codex #3).
- **Granularity**: per-changed-prose-spec nano calls, conservative single-call +
  fail-safe, no voting (Codex #4).
- **Migration**: `digest_scheme` tag + raw-digest escape hatch (Codex #5).
- **Posture**: default-on but conservative, `--no-semantic-gate` / `[semantic_gate]`,
  and **validate-before-refreeze** (Codex #6). (Codex preferred opt-in; the user
  chose default-on-conservative given the safer contract-diff strategy.)

## 10. Testing

Unit (mocked backend, no API key — matches the existing suite):
- **Normalized-digest invariance**: a spec reformatted by ruff (requote, re-wrap
  signature, trailing comma, blank lines) and comment-only edits produce an identical
  `local_digest`; an async↔sync flip, a default-value change, a rename, a class
  attribute *value* change, and a `@jaunt.preserve` body edit each change it.
- **Classifier**: structural change → MEANINGFUL with no gate call; prose-only →
  exactly one gate call.
- **Gate parse**: `EQUIVALENT` → keep; `REBUILD`/garbage/empty/timeout → MEANINGFUL.
- **Re-freeze**: body byte-identical, header digests + scheme + sidecar updated;
  validation failure forces rebuild instead of re-freeze.
- **Rollup**: dependent re-freezes when its only changed dependency was EQUIVALENT;
  rebuilds when it was MEANINGFUL.
- **Migration**: scheme-1 fresh file re-freezes silently (no gate); scheme-1 stale
  file routes through the gate.
- **Flags/config**: `--no-semantic-gate` skips the gate but keeps Layer A; `--force`
  rebuilds all; `[semantic_gate] enabled=false` disables the gate.

Integration: a prose-reworded spec with a mocked-EQUIVALENT gate is re-frozen (no
`gpt-5.5` call); a behavior-changing spec with mocked-MEANINGFUL is rebuilt.

## 11. Open questions / risks

- **Effective-signature source of truth.** The digest must use the same effective
  signature the generator/validator sees (`builder.py:~1230`, decorator analysis), not
  the raw AST args. The plan must pin the exact helper to reuse so digest and prompt
  never diverge.
- **Sidecar location & VCS.** `__generated__/` is committed; the `.contract.json`
  sidecar rides with it. Confirm during planning that committing it is acceptable
  (alternative: a single `.jaunt/contracts/` manifest), and that `jaunt clean`
  removes it.
- **`module_api_digest` includes docstrings — by design.** We keep it (dependents
  legitimately see dep docstrings); the gate handles propagation. Not changing it.
