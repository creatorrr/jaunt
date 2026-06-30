# Codex preamble + model overview (follow-up on main) — Implementation Plan

> **For agentic workers:** execute task-by-task via subagent-driven-development. Steps use `- [ ]`.

**Goal:** Add the two durable wins from the abandoned `codex-repo-context-impl` branch onto current `main` (which already shipped PR #46's treedocs repo map + colgrep retrieval and #47's builtin skills): (1) a static **Jaunt preamble** at the top of the build prompt, and (2) a digest-cached, model-written **project overview** block.

**Why this shape:** #46 already provides the structural repo map (`repo_map_block`) and retrieval; it has **no** Jaunt framework preamble and **no** prose architecture overview. So we graft only those two, reusing #46's structures rather than re-architecting the prompt. We DROP (superseded/conflicting): the Jinja prompt rewrite, our `build_project_map` (redundant with the treedocs map), the `package_context_block` removal (main keeps it), and the `jinja2` dependency.

**Tech Stack:** Python 3.12+, `codex exec`, pytest, ruff, ty. Overview prompts render via main's existing `render_template` (`{{var}}` replacer in `generate/shared.py`) — NOT jinja2.

## Global Constraints
- ruff line-length 100 (E/F/I/UP/B); must pass `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`. Project gates on **`ty`, not Pyright** — ignore Pyright-only noise.
- Full suite mocks the backend; no API key. Baseline on main = **681 passed**; keep it green.
- Reuse #46 structures: home the overview in `src/jaunt/repo_context/`, config under `[context]`, persist under `.jaunt/`, mirror `repo_map_block_for_build`'s entry-point style.
- The overview's model call reuses `CodexBackend.complete_text` (the existing read-only path). Do NOT add jinja2.
- **Defaults:** preamble is **always on** (constant, zero model cost). The model overview is **opt-in** (`[context] overview = false` by default), consistent with main's other LLM-gated context features (`enrich=false`, `search.enabled=false`). [Decision: differs from the earlier standalone "on by default"; opt-in is safer when grafting onto shipped main since it adds a per-build model call. Flip the default later if desired.]
- Do NOT `git add` anything under `.superpowers/`.
- Drive `codex exec -m gpt-5.5 -c model_reasoning_effort="medium" --sandbox workspace-write -c approval_policy="never" --skip-git-repo-check -C <worktree>` for SOURCE edits; write tests yourself.

## Anchors on main (verified)
- `_build_prompt`: `src/jaunt/generate/codex_backend.py:384` — `blocks[0]` is the `"Write a complete Python module..."` line; `repo_map_block` is appended in the block list; opening has no preamble.
- Build site: `_cmd_build_async` in `src/jaunt/cli.py` — `repo_map_block` computed at ~1328-1332; `builder.run_build(...)` called at ~1482 with `repo_map_block=...` at ~1494. (async context → can `await`.)
- Cache: `cache_key_from_context` `src/jaunt/cache.py:33`; `repo_map_block` hashed at :108, `relevant_context_block` at :110.
- Config: `[context]` → `ContextConfig` (`repo_map`, `repo_map_file`, `enrich`, `max_chars`, `search`). `ModuleSpecContext` (`generate/base.py`) has `repo_map_block`, `relevant_context_block`, keeps `package_context_block`; NO `project_overview_block`.
- Repo-map entry point to mirror: `repo_context/api.py::repo_map_block_for_build(*, root, cfg, today) -> str`.

---

## Task 1: Static Jaunt preamble

**Files:** Create `src/jaunt/prompts/codex_preamble.md`; Modify `src/jaunt/generate/codex_backend.py` (`_build_prompt`); Test `tests/test_codex_backend.py` (or a new `tests/test_codex_preamble.py`).

**Interfaces:** Produces: the build prompt now opens with the packaged preamble, loaded via `load_prompt("codex_preamble.md", <override or None>)` and prepended as `blocks[0]`.

- [ ] **Step 1 — failing test.** Add a test asserting the rendered `_build_prompt(...)` output (use the file's existing `_ctx()`/SimpleNamespace pattern) STARTS WITH the preamble and contains: `jaunt`, `spec-driven`, `signature`, `docstring`, `__generated__`, `no placeholder`. Run → RED.

- [ ] **Step 2 — preamble file (drive Codex).** Create `src/jaunt/prompts/codex_preamble.md` with this content (adapted from the salvaged template — Jaunt framing only; do NOT duplicate main's existing "spec stubs are the contract / read dep_*.pyi" line, which already follows):

```
You are generating code for Jaunt, a spec-driven code generation framework for Python.
A developer writes intent as a decorated stub — a function or class signature plus a
docstring — and Jaunt turns each stub into a real, working implementation. You are the
engine that writes that implementation.

The contract you implement is exact:
- The signature (name, parameters, type hints, return type) is the API you must match
  precisely. If the stub is `async def`, your implementation must be `async def` too.
- The docstring is the behavioral specification: the rules, edge cases, and error
  conditions you must satisfy.

What you write is real production code, not a sketch: it is written into the project's
`__generated__/` directory and imported by other generated modules and by the project's
test suite. No placeholders, no `TODO`, no stub bodies — output the complete module.
```

- [ ] **Step 3 — prepend in `_build_prompt` (drive Codex).** Load the packaged preamble and make it `blocks[0]`, before the `"Write a complete Python module..."` line. If `CodexBackend` holds a prompts config, support `[prompts] build_preamble` override (`load_prompt("codex_preamble.md", self._prompts.build_preamble or None)`); else load packaged unconditionally. The preamble is a constant → do NOT add it to any cache key.

- [ ] **Step 4 — run** the new test + `uv run pytest tests/test_codex_backend.py -q` (update any existing assertion that pinned the old opening line as blocks[0]); `uv run ruff check . && uv run ty check`. GREEN.

- [ ] **Step 5 — commit.** `feat(codex): static Jaunt preamble atop the build prompt`.

---

## Task 2: Overview machinery module + unit tests

**Files:** Create `src/jaunt/repo_context/overview.py`, `src/jaunt/prompts/project_overview_system.md`, `src/jaunt/prompts/project_overview_user.md`; Test `tests/test_repo_context_overview.py`.

**Interfaces:** Produces (in `jaunt.repo_context.overview`):
- `project_spec_digest(module_specs: dict[str, list[SpecEntry]], repo_map_block: str) -> str` — sha256 over sorted spec sources + the repo map.
- `build_project_docs_block(root: Path, *, max_chars: int) -> str` (+ `_doc_intro`) — README + AGENTS/CLAUDE intros, capped (salvaged verbatim).
- `async load_or_build_overview(backend, *, repo_map_block: str, project_docs: str, digest: str, state_dir: Path, enabled: bool, prompts) -> str` — `""` when disabled; cached `state_dir/PROJECT_OVERVIEW.md` when `state_dir/project_overview.digest` matches AND the file exists; else ONE `await backend.complete_text(system, user)`, then writes both. **Render the user prompt with `render_template` (main's `{{var}}` replacer), not jinja2.**

- [ ] **Step 1 — failing tests.** In `tests/test_repo_context_overview.py`: (a) cache-hit-skips-model — a `FakeBackend` with async `complete_text` counting calls; two `load_or_build_overview` calls, same digest → `calls == 1`; `PROJECT_OVERVIEW.md` written. (b) disabled→"" — a backend whose `complete_text` raises is never called when `enabled=False`. (c) `build_project_docs_block` reads README+AGENTS, caps, and returns "" with no docs. (d) assert `calls == 1` guard present. Run → RED (ImportError).

- [ ] **Step 2 — prompts (drive Codex).** Create the two prompt files (salvaged):
  - `project_overview_system.md`: "You are a staff engineer writing a concise architecture overview for an AI code generator. Output prose only (no code fences). Cover: what the project does, how the modules fit together, the data/dependency flow, and the conventions a generated module must follow (async style, error types, naming). Be specific and under 250 words."
  - `project_overview_user.md`: `Project docs:\n{{project_docs}}\n\nRepository map:\n{{repo_map}}\n\nWrite the architecture overview.`

- [ ] **Step 3 — module (drive Codex).** Create `repo_context/overview.py` with the three public functions (salvaged from the old branch's `project_info.py`, adapted: `render_template` instead of `render_jinja`; `repo_map_block` param name; import `load_prompt`/`render_template` from `jaunt.generate.shared`). The user-prompt mapping is `{"project_docs": project_docs, "repo_map": repo_map_block}`.

- [ ] **Step 4 — run** the new tests → GREEN; full suite `uv run pytest -q`; ruff + ty.

- [ ] **Step 5 — commit.** `feat(repo-context): digest-cached model-written project overview`.

---

## Task 3: Wire overview into ctx / prompt / cache / config / build

**Files:** Modify `src/jaunt/generate/base.py` (`ModuleSpecContext`), `src/jaunt/generate/codex_backend.py` (`_build_prompt`), `src/jaunt/cache.py` (`cache_key_from_context`), `src/jaunt/config.py` (`ContextConfig` + `[prompts]`), `src/jaunt/cli.py` (`_cmd_build_async`), `src/jaunt/builder.py` (`run_build` passthrough); Test `tests/test_cli*.py`, `tests/test_config.py`, `tests/test_cache.py`.

**Interfaces:**
- Consumes Task 2's `project_overview_block_for_build`. Add to `repo_context/overview.py`: `async project_overview_block_for_build(*, root, cfg, module_specs, repo_map_block, backend) -> str` — returns `""` if `not cfg.context.overview`; else computes digest, calls `load_or_build_overview(backend, repo_map_block=..., project_docs=build_project_docs_block(root, max_chars=cfg.context.max_chars), digest=..., state_dir=root/".jaunt", enabled=True, prompts=cfg.prompts)`.
- Produces: `ModuleSpecContext.project_overview_block: str = ""`; config `ContextConfig.overview: bool = False`; `PromptsConfig.project_overview_system/_user` (str=""); CLI passes the block through `run_build` to every ctx.

- [ ] **Step 1 — failing integration test.** Mirror main's build-test harness: real `CodexBackend`, monkeypatch `CodexBackend.complete_text` (async → `"OVERVIEW PROSE"`) and `CodexBackend.generate_module` (capture `self._build_prompt(ctx, "x.py", None)`); set `[context] overview = true`; run `jaunt build --root <proj>`; assert `"OVERVIEW PROSE" in captured["prompt"]` + a guard that capture happened. Run → RED.

- [ ] **Step 2 — config (drive Codex).** Add `overview: bool = False` to `ContextConfig` + parse block (mirror `enrich`); add `project_overview_system`/`project_overview_user` to `PromptsConfig` + parse. Add config-default + override assertions to `tests/test_config.py`.

- [ ] **Step 3 — ctx + prompt + cache (drive Codex).** Add `project_overview_block: str = ""` to `ModuleSpecContext`. In `_build_prompt`, inject it as an early block — after the preamble + the two opening lines, **before** `repo_map_block` (so order is: preamble → task lines → overview → … → repo_map). In `cache_key_from_context`, hash `ctx.project_overview_block` right after the `repo_map_block`/`relevant_context_block` lines (matching main's convention that orientation blocks participate in freshness). Add a `tests/test_cache.py` assertion that a differing `project_overview_block` changes the key.

- [ ] **Step 4 — build wiring (drive Codex).** In `_cmd_build_async`, after `repo_map_block` is computed and `module_specs` is in scope (compute just before the `run_build` call if needed), add:
  ```python
  overview_block = ""
  if cfg.context.overview:
      from jaunt.repo_context import overview as rc_overview
      overview_block = await rc_overview.project_overview_block_for_build(
          root=root, cfg=cfg, module_specs=module_specs,
          repo_map_block=repo_map_block, backend=_build_backend(cfg),
      )
  ```
  Pass `project_overview_block=overview_block` into `builder.run_build(...)`; thread it through `run_build` onto every `ModuleSpecContext` (same passthrough pattern as `repo_map_block`).

- [ ] **Step 5 — run** the integration test → GREEN; full suite green (existing build tests whose fake backends lack `complete_text` stay safe because `overview` defaults False — verify); ruff + ty.

- [ ] **Step 6 — commit.** `feat(cli): inject model-written overview into build prompt (opt-in)`.

---

## Task 4: Docs + full gate

**Files:** Modify `CLAUDE.md`; verification only otherwise.

- [ ] **Step 1.** Document in `CLAUDE.md` under `[context]`: `overview = false` (opt-in model-written architecture overview, digest-cached to `.jaunt/PROJECT_OVERVIEW.md`); and the `[prompts] build_preamble`/`project_overview_system`/`project_overview_user` overrides. Note the always-on Jaunt preamble.
- [ ] **Step 2 — full gate:** `uv run ruff check --fix . && uv run ruff format .`; `uv run ty check`; `uv run pytest`. All green.
- [ ] **Step 3 — commit.** `docs: document Jaunt preamble + opt-in project overview`.

---

## Self-Review
- Preamble (Task 1) → the user's core ask; always-on, zero model cost, no cache churn.
- Overview (Tasks 2-3) → opt-in, digest-cached, reuses #46's `repo_context`/`.jaunt`/config conventions and `complete_text`; fed by #46's `repo_map_block` + README/AGENTS (the salvaged docs block survives as an input).
- Dropped vs old branch: Jinja rewrite, `build_project_map`, `package_context_block` removal, jinja2 dep — all confirmed redundant/conflicting with #46.
- Cache: overview participates in freshness (main's convention for orientation blocks); preamble is constant and excluded.
- No placeholders; every code step shows the content or names the exact salvaged source.
