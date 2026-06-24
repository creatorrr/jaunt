# Codex as Jaunt's sole engine — Implementation Plan

> **Design:** `docs/superpowers/specs/2026-06-24-codex-engine-design.md` (read it first).
> **Supersedes:** the `2026-06-24-whole-class-aider` spec+plan. That plan's **code** for
> T1/T2/T3/T5 is reused verbatim (validation guards, scaffold/contract/imports, ctx
> fields, builder wiring); its T4 (aider escalation) and T6 (legacy fallback) are
> **dropped**.

**Goal:** Replace the engine layer with a single `CodexBackend` driving `codex
mcp-server` (delete `aider` + `legacy` + the three provider backends), delivering
whole-class `@magic` natively, in one big-bang cutover sequenced so `uv run pytest` stays
green at every commit.

## How this plan is executed (workflow harness)

Each task is run by an **Opus subagent that drives the Codex CLI** to write the code,
then **independently verifies and commits**. Two distinct Codex usages — do not conflate:

- **Dev-time (this workflow):** the subagent writes Jaunt's source by running
  `codex exec --dangerously-bypass-approvals-and-sandbox -c model_reasoning_effort="high" - < PROMPTFILE`
  (prompt via stdin to avoid quoting). The subagent never edits large code by hand — it
  directs Codex, then checks the result.
- **Runtime (the product being built):** `CodexBackend` drives `codex mcp-server`. That's
  the feature, not the dev harness.

## Global constraints

- **Run tasks strictly sequentially.** They share core files (`config.py`, `cli.py`,
  `builder.py`, `validation.py`, `base.py`, `fingerprint.py`); parallel Codex runs collide.
- **Green at every commit.** A task is done only when, from repo root:
  `uv run pytest -q && uv run ruff check . && uv run ty check` all pass. Ruff: line-length
  100, rules E/F/I/UP/B, Python 3.12+.
- **Additive before destructive.** Codex stays *selectable alongside* aider/legacy through
  Tasks 1–7; the default flip + all deletions happen in Task 8. This keeps every
  intermediate commit green.
- **The mock seam is sacred.** Unit tests mock `GeneratorBackend.generate_with_retry`;
  `CodexBackend` inherits it, so existing tests must keep passing without rewrites (except
  config/engine-enum tests, updated in Task 8).
- **Verify behavior, not just mocks.** The whole-class gap only shows on a real build
  (`aider-whole-class-gap`); Task 10 is a mandatory real Codex E2E.
- Subagents: use a generous Bash timeout for `codex exec` (run in background if a task is
  large), iterate Codex until verification passes (bounded retries), then `git add -A &&
  git commit` with the message under each task. Do **not** push.
- If a subagent hits a genuine blocker (ambiguous spec, Codex repeatedly failing
  verification), it must stop and report rather than commit broken/red code.

---

## Task 1 — `CodexBackend` + `[codex]` config (additive)

**Read:** spec §5 (CodexBackend), §6.1 (config); skill refs
`.claude/skills/codex-harness/references/jaunt-integration.md` (worked backend sketch) and
`mcp-server.md` (the `codex`/`codex-reply` tools).

**Implement:**
- Add `mcp` to `pyproject.toml` dependencies. **Do not** remove `aider-chat` yet.
- `src/jaunt/config.py`: add a `CodexConfig` dataclass (`model`, `reasoning_effort`,
  `sandbox`, optional `features`, optional raw `config` dict) + `[codex]` table parsing,
  parallel to `AiderConfig`. Add `"codex"` to `_VALID_AGENT_ENGINES` →
  `("legacy", "aider", "codex")`. **Keep** the default `engine = "aider"` for now.
- `src/jaunt/generate/codex_backend.py` (new): `CodexBackend(GeneratorBackend)` driving
  `codex mcp-server` via the `mcp` Python SDK, with a **session pool sized to
  `build.jobs`** (lazy-start, checkout via `asyncio` queue/semaphore, `aclose()` tears
  down). Implement `generate_module` (temp-workspace seed → context files → `codex` tool
  call with `cwd`/`sandbox=workspace-write`/`approval-policy=never`/model/effort → read
  target file back → `(source, usage|None)`), `complete_text` (read-only `codex` call →
  final agent message), `provider_name="codex"`, `model_name`,
  `supports_structured_output=False`.
- `src/jaunt/cli.py` `_build_backend`: add an `engine == "codex"` branch →
  `CodexBackend(cfg.codex, cfg.llm, cfg.prompts)`.

**Tests:** `tests/test_codex_backend.py` (new) with the `mcp` `ClientSession` mocked:
seed/read-back, prompt assembly (spec/dep/contract files + the "edit only the target"
constraint + `extra_error_context`), `complete_text`, pool checkout/return + `aclose`,
usage → `TokenUsage | None`. Extend `tests/test_config.py` for `[codex]` parsing + the
enum.

**Verify & commit:** `feat(codex): CodexBackend on mcp-server + [codex] config (selectable engine)`

---

## Task 2 — Validation guards (whole-class hardening, backend-agnostic)

**Read:** whole-class-aider plan **Task 1** (use its code verbatim) + spec §4.2.

**Implement:** in `src/jaunt/validation.py`, extend `validate_build_class_source` with the
three guards (AST unfilled-stub via `class_analysis.is_stub_body`, docstring-only
completeness `require_public_method`, attribute preservation `class_attributes:
dict[str,str]`) + `_class_attribute_nodes`. New params defaulted (back-compatible).

**Tests:** extend `tests/test_validation_class.py` per the aider plan Task 1.

**Verify & commit:** `feat(validation): unfilled-stub, docstring-only, attribute guards for whole-class`

---

## Task 3 — Scaffold builder, import collector, contract renderer

**Read:** whole-class-aider plan **Task 2** (code verbatim, including the Codex-review
notes about the `pass` emission and the dropped `# preserved` marker).

**Implement:** append to `src/jaunt/class_analysis.py`: `collect_spec_module_imports`,
`build_class_scaffold`, `render_whole_class_contract` + private helpers.

**Tests:** create `tests/test_class_scaffold.py` per the aider plan Task 2.

**Verify & commit:** `feat(class_analysis): scaffold builder, import collector, whole-class contract renderer`

---

## Task 4 — `ModuleSpecContext` fields + cache key

**Read:** whole-class-aider plan **Task 3** (code verbatim).

**Implement:** add `seed_target_content: str = ""`, `whole_class_contract_block: str =
""`, `whole_class: bool = False` to `ModuleSpecContext` (`src/jaunt/generate/base.py`);
mix all three into `cache_key_from_context` (`src/jaunt/cache.py`).

**Tests:** extend/create `tests/test_cache.py` per the aider plan Task 3.

**Verify & commit:** `feat(context): carry scaffold seed + whole-class contract on ctx and cache key`

---

## Task 5 — Builder wiring + in-loop validator + CodexBackend consumes seed/contract

**Read:** whole-class-aider plan **Task 5** (builder wiring portion) + spec §4.2, §5.3,
§5.6. **Skip** the aider-plan Tasks 4 and 6 entirely (no aider seeding, no
escalation, no fallback backend).

**Implement:**
- `src/jaunt/builder.py`: per-component, build `seed_target_content` (imports +
  `build_class_scaffold` per whole-class spec) and `whole_class_contract_block`
  (`render_whole_class_contract`) into `ModuleSpecContext`; set `whole_class=bool(whole)`.
  Populate the new `_class_validation_inputs` keys (`class_attributes`,
  `require_public_method`) and make the **retry validator class-aware** (run
  `validate_build_class_source` inside the loop so a stub/missing-method failure
  re-prompts via `extra_error_context`).
- `src/jaunt/generate/codex_backend.py`: in `generate_module`, write
  `ctx.seed_target_content` as the seed at the target path (empty → Codex writes fresh),
  and `ctx.whole_class_contract_block` as `_context/whole_class_contract.md`; reference
  both in the prompt ("implement every `# jaunt:implement` method; keep preserved methods
  verbatim").

**Tests:** add a `tests/test_builder_whole_class.py` recording-backend test (per aider plan
Task 5, minus fallback) asserting the ctx carries the scaffold/flag and the in-loop
validator rejects an unfilled stub. Add a `CodexBackend` test asserting the seed + contract
file are written.

**Verify & commit:** `feat(builder,codex): seed scaffold/contract into ctx + class validator in retry loop`

---

## Task 6 — `CodexExecutor` + rewire auto-skill generation

**Read:** spec §7 (Rewire — D8); `src/jaunt/agent_runtime.py` (`AgentExecutor` ABC),
`src/jaunt/skill_builder.py`, `src/jaunt/skills_auto.py`, `src/jaunt/aider_executor.py`
(the interface to mirror).

**Implement:** add a `CodexExecutor(AgentExecutor)` (drive `codex mcp-server`/`CodexBackend`
to materialize an `AgentTask` and return edited target content). Re-point
`skill_builder.py` / `skills_auto.py` so `engine == "codex"` uses `CodexExecutor`; keep the
existing aider/provider branches working for now (additive). Drop the `skill_mode`
dependency for the Codex path.

**Tests:** unit test `CodexExecutor` (mocked) + a skills-path test selecting Codex.

**Verify & commit:** `feat(skills): CodexExecutor and Codex-driven auto-skill generation`

---

## Task 7 — Defer `jaunt eval` + update `jaunt init` template

**Read:** spec §7 (Defer — D9; init template); `src/jaunt/eval.py`, the `eval`/`init`
commands in `src/jaunt/cli.py`.

**Implement:**
- Make `jaunt eval` exit with a clean "not supported under the Codex engine (rework
  pending)" error. Ensure `eval.py` does **not** import the (soon-deleted) provider
  backends at module load — lazy-import or stub so `cli.py` still imports cleanly after
  Task 8.
- Update the `jaunt init` scaffolded `jaunt.toml` to emit `[agent] engine = "codex"` + a
  `[codex]` block instead of aider/provider defaults.

**Tests:** update `tests/test_cli.py` (or equivalent) for the eval stub + init template.

**Verify & commit:** `feat(cli): defer jaunt eval under Codex; scaffold [codex] in init`

---

## Task 8 — Cutover: flip default to Codex + delete aider/legacy (destructive)

**Read:** spec §6.1, §7, §5.7 (fingerprint). This is the heaviest task; the subagent may
need several Codex passes — keep iterating until the suite is fully green.

**Implement:**
- Flip default `engine = "codex"`; set `_VALID_AGENT_ENGINES = ("codex",)`; make config
  validation reject `legacy`/`aider` with an actionable error (remove `[agent]`/`[aider]`).
- Delete: `aider_backend.py`, `aider_executor.py`, `aider_contract.py`, `openai_backend.py`,
  `anthropic_backend.py`, `cerebras_backend.py`; `AiderConfig` + `[aider]` parsing
  (incl. `skill_mode`); the aider + provider branches in `_build_backend`; the
  `--interactive` flag, its `cli.py:1330` guard, and `generate_interactive`.
- `generation_fingerprint_from_config` (`fingerprint.py`): replace the aider-only
  model/effort branch with a `codex` branch mixing in `cfg.codex.model`,
  `reasoning_effort`, `sandbox`; remove `aider_generation_fingerprint_parts` usage.
- Remove `aider-chat` from `pyproject.toml`; update `uv.lock` (`uv sync`).
- Sweep every residual import/use of the deleted symbols (incl. `skill_builder.py`,
  `skills_auto.py`, `eval.py`, tests). Update all engine/provider config tests to the
  Codex-only world.

**Verify & commit:** `feat!: Codex is the sole engine — remove aider + legacy backends`

---

## Task 9 — Example, docs, memory

**Read:** spec §10.

**Implement:**
- `examples/06_whole_class/jaunt.toml` + `README.md`: drop the `[agent] engine = "legacy"`
  pin + note (default engine now generates whole classes).
- Add a "superseded by `2026-06-24-codex-engine-design.md`" header to the whole-class-aider
  spec + plan.
- `CLAUDE.md` / `README.md`: replace provider-switch / "OpenAI or Anthropic" language with
  the Codex engine model; document the `codex` install + `codex login` requirement; update
  config + CLI docs (drop `--interactive`, `[aider]`; add `[codex]`; note `jaunt eval`
  deferred).
- Memory (via the memory tool, not committed): `aider-whole-class-gap` → closed-via-Codex;
  `aider-decision` → superseded; record Codex-as-engine + mcp-server-pool; refresh
  `MEMORY.md`.

**Verify & commit:** `docs: Codex engine docs; whole-class example drops legacy pin`

---

## Task 10 — Real Codex E2E (mandatory)

**Read:** spec §9; whole-class-aider plan Task 7 Step 4 (run shape).

**Implement / run:** from `examples/06_whole_class`, run a **real** `jaunt build` under the
Codex engine (uses the local `codex login` session). Confirm: `Stack` stubs implemented,
`is_empty` body **verbatim**, `Inventory` designed from its docstring, `TempStats` present;
then `jaunt test` battery passes. If output reveals a prompt/seed gap, fix it in
`codex_backend.py` / `class_analysis.py` (not by re-pinning legacy) and re-run.

**Verify & commit:** `test(e2e): whole-class build under Codex engine on 06_whole_class`

---

## Done criteria

- `engine = "codex"` is the only valid engine; aider/legacy code, config, deps, and
  `--interactive` are gone; `jaunt eval` is a clean deferred stub.
- Whole-class `@magic` builds under the default engine, preserved methods verbatim, proven
  by a real E2E.
- `uv run pytest -q && uv run ruff check . && uv run ty check` green on every commit.
