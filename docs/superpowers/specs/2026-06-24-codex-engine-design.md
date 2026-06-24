# Codex as Jaunt's sole code-generation engine — Design

**Date:** 2026-06-24
**Status:** Accepted (design); implementation pending
**Supersedes:** `docs/superpowers/specs/2026-06-24-whole-class-aider-design.md` and
`docs/superpowers/plans/2026-06-24-whole-class-aider.md` (whole-class is delivered
natively by Codex; see §3). Those documents are retained for their Codex-review
history but should carry a "superseded by this spec" header.

---

## 1. Context & motivation

Jaunt currently selects a code-generation backend via `[agent] engine`, validated
against `_VALID_AGENT_ENGINES = ("legacy", "aider")` (default `aider`). The factory
is `_build_backend()` at `src/jaunt/cli.py:528`:

- `engine = "aider"` → `AiderGeneratorBackend` (drives aider via litellm; also powers
  `jaunt build --interactive` at `cli.py:1330`).
- `engine = "legacy"` → one of the direct provider backends
  (`OpenAIBackend` / `AnthropicBackend` / `CerebrasBackend`) selected by `[llm] provider`.

Two problems motivate the switch to OpenAI's **Codex** CLI:

1. **The whole-class gap.** Whole-class `@jaunt.magic` (docstring-only / stubs / mix)
   only works under `engine = "legacy"`, because aider's SEARCH/REPLACE edit flow can't
   emit a fresh full class body (`aider-whole-class-gap` memory). Codex emits **whole
   files**, which is the native fix.
2. **Aider's fragility.** The original adoption note (`aider-decision`) already flagged
   aider's scripting API as "explicitly marked as unsupported/unstable" and its
   dependency footprint as heavy. The `legacy` backends exist largely as a
   multi-provider / test fallback.

Codex is highly scriptable (`exec`, `mcp-server`, `app-server`) and is the user's
established hands-on coding harness (`workflow-codex-execution` memory). Replacing the
engine layer with Codex unifies Jaunt's generation on the same tool the user already
drives, and **collapses** most of the pending whole-class-aider machinery (§3).

The full Codex usage reference lives in the `codex-harness` skill
(`.claude/skills/codex-harness/`), grounded against codex-cli 0.142.0.

## 2. Goals / Non-goals

**Goals**
- One generation engine: `CodexBackend`, driving `codex mcp-server`.
- Whole-class `@jaunt.magic` works under the (only) default engine, with
  `@jaunt.preserve` bodies surviving verbatim.
- `jaunt reconcile` (contract derivation) keeps working, via Codex `complete_text`.
- Remove `aider` + `legacy` engines and their backends in a single cutover (big-bang).
- Builder parallelism (`build.jobs`) preserved.

**Non-goals (this iteration)**
- True multi-provider (Anthropic/Cerebras) generation. Deferred: the `[llm]` schema
  stays provider-shaped so Codex `model_providers` routing can be added later without a
  breaking change, but Codex targets OpenAI models now (§4).
- `jaunt build --interactive`. Dropped (§6); Codex's own TUI covers interactive use.
- `app-server` integration (per-item events/approvals). Not needed for "produce one
  module"; `mcp-server` is sufficient.
- `jaunt eval` rework. Deferred to a disabled stub now; re-targeting it at Codex
  model/effort comparison is a separate future effort (§7, D9).

## 3. Locked decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Codex is the sole engine**; delete `aider` + `legacy`. | Smallest surface; aider is unstable; legacy was mostly a fallback. |
| D2 | **Defer multi-provider**: build provider-agnostic, target OpenAI now, keep `[llm]` schema. | Ship the whole-class win fast; don't paint the config into an OpenAI-only corner. |
| D3 | **Drive `codex mcp-server`** via the `mcp` Python SDK (long-lived). | Process reuse, structured I/O, thread-native; enables future generate→critique→fix. |
| D4 | **Session pool sized to `build.jobs`**. | Guarantees real parallelism + failure isolation regardless of the server's internal thread scheduling. |
| D5 | **Drop `jaunt build --interactive`**. | Aider-coupled; Codex TUI covers it. |
| D6 | **Big-bang cutover** (one plan adds Codex and deletes aider+legacy together). | Faster to the clean end-state; user accepts no intermediate fallback. |
| D7 | **Fold in whole-class hardening (T1/T2/T3/T5), drop T4/T6.** | Codex makes the SEARCH/REPLACE workarounds (escalation, legacy fallback) unnecessary. |
| D8 | **Rewire auto-skill generation to Codex** (keep it running in `build`). | It runs in the build path; disabling would change build output. Preserve via a `CodexExecutor`. |
| D9 | **Defer `jaunt eval`** to a disabled stub. | "Compare providers" is meaningless under Codex-only; reworking it to compare Codex models/efforts is out of scope now. |

## 4. Architecture overview

### 4.1 The cutover

```
BEFORE                              AFTER
engine ∈ {legacy, aider}           engine = codex   (only value)
  aider → AiderGeneratorBackend      → CodexBackend (drives `codex mcp-server`)
  legacy → {OpenAI,Anthropic,
            Cerebras}Backend
```

`_build_backend(cfg)` becomes: always construct `CodexBackend(cfg.codex, cfg.llm,
cfg.prompts)`. The `[llm] provider` branch and the aider branch are removed. There is no
`_build_fallback_backend` (the whole-class-aider T6 fallback is dropped — Codex *is* the
whole-file generator that fallback provided).

### 4.2 Whole-class inheritance (why T4/T6 vanish)

A whole-class build branch already exists (commits `1faaf11`, `c74133e`, `3c6f716`):
`_whole_class_specs` (`builder.py:279`), `classify_class_mode` / `split_class_members`
in `class_analysis.py`, base-class contract resolution, and a **post-merge**
`validate_build_class_source` (`validation.py:429`). This branch already works for any
whole-file backend — which is exactly why whole-class works under `legacy` today.

**Codex is a whole-file backend, so it inherits that branch the way `legacy` does.** The
pending whole-class-aider plan therefore reduces to its backend-agnostic *hardening*:

| whole-class-aider task | Under Codex |
|---|---|
| **T1** validation guards (unfilled-stub AST, docstring-only completeness, attribute preservation) | **KEEP** — harden Codex output, run **in-loop** so failures re-prompt |
| **T2** `render_whole_class_contract` | **KEEP** — contract prose in the prompt + a `context/` file |
| **T2** `build_class_scaffold` + `collect_spec_module_imports` | **ADAPT** — used as Codex's **seed file** (guarantees `@jaunt.preserve` bodies survive verbatim) |
| **T3** ctx fields + cache-key mixing | **KEEP** — Codex backend consumes them |
| **T5** builder wiring + **in-loop** class validator | **KEEP** — re-validate Codex output inside the retry loop |
| **T4** aider seed + diff→whole-file **escalation** | **DROP** — Codex is always whole-file |
| **T6** **legacy fallback** (cache-bypass, fingerprint stamping) | **DROP** — no fallback engine exists |

## 5. Component: `CodexBackend(GeneratorBackend)`

New file: `src/jaunt/generate/codex_backend.py`.

### 5.1 Interface satisfied

From `src/jaunt/generate/base.py`:

- `async def generate_module(self, ctx: ModuleSpecContext, *, extra_error_context=None) -> tuple[str, TokenUsage | None]` — the one required method.
- Inherited `generate_with_retry(ctx, max_attempts=...)` validates and re-prompts via
  appended `extra_error_context`. **No Codex-side retry loop.**
- Properties: `model_name` (codex model), `provider_name = "codex"`,
  `supports_structured_output` (see §5.5).
- `async def complete_text(self, *, system: str, user: str) -> str` — required for
  `jaunt reconcile` (§5.4).
- `async def aclose(self)` — tears down the session pool (the builder owns backend
  lifetime).

### 5.2 Driving `codex mcp-server` + the session pool

Each pool slot is one `codex mcp-server` subprocess wrapped in an `mcp.ClientSession`
over stdio (`mcp.client.stdio.stdio_client` + `mcp.StdioServerParameters(command="codex",
args=["mcp-server"])`), initialized once. The pool:

- Size = `cfg.build.jobs` (cap at the module count; floor 1).
- **Lazy start** — spawn a slot on first checkout, so a single-module build doesn't
  launch N processes.
- Checkout via an `asyncio.Queue`/semaphore; `generate_module` holds a slot for the
  duration of one `codex` tool call, then returns it.
- `aclose()` closes every started slot's `AsyncExitStack`.

> The pool is the D4 hedge: even if a single `mcp-server` serializes threads internally,
> N processes give N-way parallelism. Fallback (if one server is found to handle
> concurrent `codex` calls cleanly) is pool size 1 + concurrent tool calls — a config
> knob, not a redesign.

### 5.3 `generate_module` flow (the impedance match)

Codex *edits files on disk*; `generate_module` must *return source text*. Per call:

1. Create a fresh `TemporaryDirectory` (isolated per concurrent call).
2. Compute the target path from `ctx.generated_module` (dotted → path + `.py`).
3. **Seed** the target with `ctx.seed_target_content` (the whole-class scaffold; empty
   string for function-only modules → Codex writes from scratch).
4. Write read-only context files: `ctx.spec_sources` → `_context/spec_*.py`,
   `ctx.dependency_apis` → `_context/dep_*.pyi`, and the whole-class contract
   (`ctx.whole_class_contract_block`) → `_context/whole_class_contract.md` when present.
   (`SpecRef` is a `str`; index filenames to avoid collisions and embed the ref as a
   leading comment.)
5. Call the `codex` tool:
   ```python
   await session.call_tool("codex", {
       "prompt": prompt,                 # §5.6
       "cwd": str(tmp),
       "sandbox": cfg.codex.sandbox,     # "workspace-write"
       "approval-policy": "never",       # headless
       "model": self._model,
       "config": {"model_reasoning_effort": cfg.codex.reasoning_effort},
   })
   ```
6. **Read the target file back** → return `(source, usage)`. The builder writes the
   source into `__generated__/` and stamps the header itself — Codex never touches the
   real repo, so it can't fight header-stamping or the response cache.

`generate_with_retry` re-invokes `generate_module` with `extra_error_context` on
validation failure (fresh `codex` call per attempt; threadId continuation is available
but unused for v1).

### 5.4 `complete_text` (keeps `reconcile` alive)

`jaunt reconcile` (`cli.py:779`) obtains its model via `_build_backend(cfg)` and calls
`backend.complete_text(system=, user=)`. Implement it as a `codex` tool call with
`sandbox = "read-only"`, `approval-policy = "never"`, a prompt that concatenates the
system + user text and instructs Codex to **return only the requested text and run no
commands**; read the final agent message from the tool result's `structuredContent`.
Codex is an agent rather than a raw completion endpoint, so this is heavier than a chat
completion, but it is acceptable for contract derivation and runs read-only.

### 5.5 Token usage & structured output

- The mcp tool result may not surface exact prompt/completion counts. Return a
  `TokenUsage` when available, else `None` (the cost tracker already tolerates `None`).
  Do not fabricate counts; real usage is visible per-account in the OpenAI dashboard.
- `supports_structured_output`: default `False` for v1 (Codex returns whole modules /
  agent text, not provider-native structured outputs). Revisit if a contract path needs
  it.

### 5.6 Prompt assembly

Reuse the pre-rendered blocks Jaunt already assembles on `ModuleSpecContext`
(`build_instructions_block`, `module_contract_block`, `base_contract_block`,
`blueprint_source`, `package_context_block`, `skills_block`, …). The Codex prompt:

- Names the target file and the `expected_names` it must export.
- Points at `_context/spec_*.py` (the behavioral contract) and `_context/dep_*.pyi`.
- For whole-class: points at `_context/whole_class_contract.md` ("implement every
  `# jaunt:implement` method; keep preserved methods verbatim; design the public API the
  docstring implies").
- **Constrains the agent**: "edit ONLY the target file; do not create other files, run
  tests, or modify anything else; output the full module — no placeholders."
- Appends `extra_error_context` (from `generate_with_retry`) when present.

### 5.7 Generation fingerprint

`generation_fingerprint_from_config` (`fingerprint.py:41`) already keys on
`engine=cfg.agent.engine` and carries model/effort slots (today populated only for
aider). Add a `codex` branch so the digest mixes in `cfg.codex.model`,
`cfg.codex.reasoning_effort`, and `cfg.codex.sandbox` — so changing any of them
invalidates incremental-build digests. (The backend's per-ctx `generation_fingerprint`
method is not used by the build flow; headers/cache use this config function.)

## 6. Config, auth & dependencies

### 6.1 Config schema

- `config.py`: `_VALID_AGENT_ENGINES = ("codex",)`; `AgentConfig.engine = "codex"`.
- New `CodexConfig` dataclass + parser (parallel to `AiderConfig`):
  ```toml
  [agent]
  engine = "codex"

  [codex]
  model = "gpt-5.2-codex"
  reasoning_effort = "high"      # matches the user's standing default
  sandbox = "workspace-write"
  # features = ["multi_agent"]   # optional --enable passthrough
  # config = { ... }             # optional raw config.toml overrides
  ```
- `[llm]` schema is **retained but informational** under Codex (D2): `[llm].model` may
  feed Codex when `[codex].model` is unset; `provider` / `api_key_env` do not dispatch a
  backend. Document this clearly and mark the seam where `model_providers` routing slots
  in later. Optionally warn when `provider != "openai"` (we target OpenAI now).

### 6.2 Auth & CI

- Codex authenticates via its own mechanism — `codex login` → `~/.codex/auth.json`
  (already present locally per `workflow-codex-execution`), or `CODEX_API_KEY`. This is
  **not** Jaunt's `[llm].api_key_env`.
- `jaunt check` stays deterministic / API-free (unchanged). `build` / `test` /
  `reconcile` require Codex auth. CI uses `CODEX_API_KEY`; never expose it to steps that
  run untrusted code.

### 6.3 Dependencies (a notable shift)

- **Add** the `mcp` Python SDK to `pyproject.toml`.
- **Remove** `aider-chat` (heavy; the `aider-decision` note flagged its footprint).
- **New external runtime requirement:** the `codex` CLI binary must be installed
  (npm/brew) and authenticated. Unlike aider (`pip install`), Codex is not a Python
  dependency — `uv sync` no longer provisions the agent. Document install + `codex
  login` in CLAUDE.md/README and surface a clear diagnostic when `codex` is missing on
  PATH (lean on `codex doctor`).

## 7. Deletions, rewires & secondary surfaces (big-bang)

**Delete (generators):** `aider_backend.py`, `aider_executor.py`, `aider_contract.py`,
`openai_backend.py`, `anthropic_backend.py`, `cerebras_backend.py`.

**Delete (config/CLI):** `AiderConfig` + `[aider]` parsing (incl. `skill_mode`); the
aider + provider branches in `_build_backend`; aider fingerprint parts
(`aider_generation_fingerprint_parts`) once the `codex` branch replaces them; the
`--interactive` flag, its `cli.py:1330` guard, and `generate_interactive`.

**Rewire — auto-skill generation (D8).** `skill_builder.py` / `skills_auto.py` currently
branch `engine == "aider"` → `AiderExecutor`, else provider backends, to generate PyPI
helper skills during `jaunt build` (`cli.py:1202`) and `jaunt skill refresh`. Re-point
them at Codex: implement `CodexExecutor(AgentExecutor)` (the `AgentExecutor` ABC in
`agent_runtime.py` stays) or reuse `CodexBackend`. The aider-only `skill_mode` edit-mode
concept is dropped (Codex is whole-file). Auto-skills keep working in the build.

**Defer — `jaunt eval` (D9).** Keep `eval.py` but make `jaunt eval` exit with a clean
"not supported under the Codex engine (rework pending)" error. Ensure `eval.py` does not
import the deleted provider backends at module load (lazy-import or stub) so `cli.py`
still imports cleanly; the rework (compare Codex models/efforts) is a separate future
effort.

**`jaunt init` template.** Update the scaffolded `jaunt.toml` (`cli.py` ~633) to emit
`[agent] engine = "codex"` + a `[codex]` block instead of the aider/provider defaults.

**Sweep** for residual imports/usages of every deleted symbol; the suite (mocked at the
`GeneratorBackend` level, §9) must stay green.

## 8. Parity

- **`reconcile` / contract mode:** KEPT via Codex `complete_text` (§5.4). Net-positive —
  works under the default engine for the first time.
- **`jaunt build --interactive`:** DROPPED (D5).
- **Incremental builds / `status`:** preserved via the fingerprint extension (§5.7).
- **JSON output, exit codes, cost tracking:** unchanged (`None` usage tolerated).

## 9. Testing strategy

- **Mock seam survives.** Existing unit tests mock `GeneratorBackend.generate_with_retry`,
  which `CodexBackend` inherits — no test rework for the swap itself.
- **`CodexBackend` units** (mock the `mcp` `ClientSession`): temp-workspace seed +
  read-back, prompt assembly (spec/dep/contract files, constraint line, error context),
  `complete_text` (read-only call → text), pool checkout/return + `aclose`, usage
  parsing → `TokenUsage | None`.
- **Whole-class hardening units:** port the plan's T1 (validation guards), T2
  (scaffold/contract/imports), T3 (ctx fields + cache key), T5 (builder wiring +
  in-loop validator) tests; drop the T4/T6 tests.
- **Concurrency smoke:** N modules through an M-slot pool complete and isolate failures.
- **E2E** (`examples/06_whole_class`, real `jaunt build` under Codex using the local
  `codex login` session): Stack stubs filled, `is_empty` body **verbatim**, Inventory
  designed, TempStats present; `jaunt test` battery passes. The `aider-whole-class-gap`
  memory's warning holds — unit tests mock generation, so the gap only shows on a real
  build; this E2E is mandatory before declaring the gap closed.

## 10. Docs & memory

- Add a "superseded by `2026-06-24-codex-engine-design.md`" header to the
  whole-class-aider spec + plan.
- `examples/06_whole_class`: remove the `[agent] engine = "legacy"` pin + README note
  (default engine now generates whole classes).
- CLAUDE.md / README: replace "OpenAI or Anthropic" / provider-switch language with the
  Codex engine model + the `codex` install/auth requirement; update the config and
  CLI-flag docs (drop `--interactive`, `[aider]`; add `[codex]`).
- Memory: `aider-whole-class-gap` → closed-via-Codex; `aider-decision` → superseded by
  Codex; record Codex-as-engine + mcp-server-pool as the chosen architecture; refresh
  `MEMORY.md` index lines.

## 11. Migration / cutover

Big-bang (D6), but the *commits* within the plan should still stack so the suite stays
green at each step: (a) `CodexConfig` + `mcp` dep; (b) `CodexBackend` + factory rewire;
(c) whole-class hardening fold-in (T1/T2/T3/T5); (d) `CodexExecutor` + rewire auto-skills,
defer `jaunt eval`, update the `init` template; (e) delete aider/legacy + `--interactive`
+ aider dep; (f) example + docs/memory + E2E. No `engine` migration shim is needed —
existing configs naming `legacy`/`aider` should fail config validation with a clear
"engine must be 'codex'" error (and a hint to remove the `[agent]`/`[aider]` blocks).

## 12. Risks & open questions

- **mcp-server concurrency model is unverified.** Mitigated by the pool (D4); confirm in
  the concurrency smoke test before tuning pool size.
- **Codex as a contract-derivation completion** is heavier/less deterministic than a chat
  completion. Read-only sandbox + a tight "return only text" prompt; revisit if
  `reconcile` output is noisy.
- **External binary dependency.** Builds now require `codex` installed + authenticated;
  CI and contributor setup must provision it. Clear missing-binary diagnostics required.
- **Determinism.** Pin `[codex].model` + `reasoning_effort`; avoid `--search` for
  reproducible builds unless a spec needs the web.
- **Token usage may be absent** under mcp-server → cost reports can be partial.
- **Existing-config breakage** is intentional (no `legacy`/`aider` shim); the error must
  be actionable.

## 13. Self-review

- **Placeholders:** none; every section is concrete. Exact `mcp` result shape
  (`structuredContent` keys) and the precise `generation_fingerprint_from_config` edit
  are flagged as implementer-verify points, not TBDs.
- **Consistency:** D1–D7 match §§4–11; "drop T4/T6 / keep T1/T2/T3/T5" is stated
  identically in §3, §4.2, and §9.
- **Scope:** single implementation plan (big-bang, six stacked commit groups);
  multi-provider, `jaunt eval` rework, and `app-server` explicitly deferred (not silently
  dropped); secondary agent-coupled surfaces (auto-skills, eval, `init` template) are
  enumerated in §7.
- **Ambiguity:** "provider-agnostic / OpenAI now" pinned in §2 + §6.1 (schema kept,
  doesn't dispatch); "pool sized to jobs" pinned in §5.2 with the single-server fallback
  called out.
