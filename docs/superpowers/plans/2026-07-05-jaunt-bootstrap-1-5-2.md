# jaunt 1.5.2 Implementation Plan — self-hosting jaunt on jaunt ("bootstrap")

> **For agentic workers:** executed by a dynamic Workflow of opus@medium subagents driving `codex exec` (model `gpt-5.5`, reasoning_effort=medium) in a dedicated worktree (branch `feat/self-hosting`). Each task = one workflow unit with its own gate and commit. `jaunt build` invocations inside conversion tasks use the root jaunt.toml (`[codex]` gpt-5.5@high) — deliberate, distinct from the agents' own codex effort.

**Goal:** jaunt builds jaunt. Convert 7 framework modules to `jaunt.magic_module` specs with committed `__generated__/` + `.pyi` output, put `@jaunt.contract` batteries on 15 core modules, fix the registry split-brain bug that makes self-discovery return zero specs, ship as 1.5.2.

**Context — why this needs framework fixes first:**

- **Bug 1 (BLOCKER, verified):** with `source_roots=["src"]`, discovered specs are `jaunt.*` names, and `evict_modules_for_import` (`src/jaunt/discovery.py:110-159`) purges the RUNNING jaunt package from `sys.modules` (exact + parent + `"jaunt."`-prefix + `__file__`-under-roots rules). `import_and_collect` then re-executes `jaunt/__init__.py`, creating a fresh registry (R2); registrations land in R2 while the CLI reads the stale pre-eviction registry reference (R1, bound at `cli.py:2165` before `clear_registries()` at `:2172`) → **zero specs discovered**. Ten clear/evict/collect sites: `cli.py` :724, :1891-1892, :1912-1917, :1941-1946, :2172-2182, :3435-3445, :3877-3887, :3961-3979, :4788-4798.
- **Bug 2 (BLOCKER, verified):** `.gitignore:8` `**/__generated__/` blocks committing generated output, and hatchling honors VCS ignores (`pyproject.toml:38-43`) → a wheel would ship stubs forwarding to nothing.
- **Cascade exclusion (verified):** `errors`, `runtime`, `module_magic`, `decorator_analysis`, `class_analysis`, `registry`, `spec_ref`, `paths` execute during `import jaunt` itself, before `jaunt.magic_module`/`jaunt.contract` are bound — they can NEVER bear jaunt decorators. Build-critical modules can never be magic stubs (the builder needs them to build them) — those get contract mode.
- Key mechanics verified: generated layout `jaunt.contract.strength` → `src/jaunt/__generated__/contract/strength.py` (`paths.py:8-16`); runtime resolution is by module name (`module_magic.py:293-298`), no jaunt.toml needed at runtime → wheel-safe once files ship. Stub `.pyi` freshness is AST-normalized over spec+generated sources (`stub_emitter.py:38-50`, :186-211) — repo-wide `ruff format` cannot restale committed stubs, so no `.pyi` excludes needed. `test_roots=[]` passes config validation (`config.py:749`). `jaunt reconcile` has no `--target` (`cli.py:451-454`) → one serialized reconcile task. `module_magic.py:182,:192`: decorated defs are never governed (heldout's pytest hooks are safe). `eval_agent_cases.py`'s SKILL.md "reads" are inside string-literal fixtures — no import-time I/O; the self-discovery sweep over framework files is harmless post-carve-out (cached no-op imports). `[paths]` has no `exclude` key and doesn't need one.

## Global Constraints

- Codex strictly `gpt-5.5` for every invocation (only the `[semantic_gate]` judge is `gpt-5.4-mini`).
- Full gate before every commit: `uv run pytest && uv run ruff check . && uv run ruff format . && uv run ty check`. Conversion tasks (T3-T9) additionally require `uv run jaunt check` exit 0.
- Conventional commits ending `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- **Every conversion/contract-wave task agent (T3-T14) must Read at task start:** `jaunt-claude-plugin/skills/working-with-jaunt/SKILL.md` and `jaunt-claude-plugin/skills/build/SKILL.md` (the distilled adopter playbook). The plugin directory itself (untracked) gets NO work in this PR. Do not touch FEEDBACK.md.
- Iron rules apply to us too: never edit `__generated__/**` or `.pyi`; fix forward through the docstring; **surface build advisories VERBATIM in every task report** (they are this PR's bug-hunting payload); never edit existing tests to accommodate generation.
- Long codex-backed builds run in ONE Bash command: `nohup uv run jaunt build --target <mod> --progress none > /tmp/jaunt-build-<slug>.log 2>&1 & tail --pid $! -f /dev/null`, then read the log fully.
- Max 2 conversion tasks in parallel (JAUNT_LOG is an unlocked append + git-index contention); stage explicit paths; retry once after 5s on index.lock.
- Budget: ~$45-120 model spend expected across the campaign; hard-stop and report if trending past $150.

## Wave map

| Wave | Tasks | Parallel |
|------|-------|----------|
| 1 | T1 registry split-brain fix · T2 infra/packaging | 2 (disjoint files) |
| 2 | T3 canary conversion: guard.py + end-to-end pipeline proof | serial — **STOP workflow on any failure** |
| 3 | T4 heldout.py · T5 migrate.py | 2 |
| 4 | T6 contract/strength.py · T7 contract/cases.py | 2 |
| 5 | T8 contract/drift.py · T9 contract/edits.py | 2 |
| 6 | T10-T13 contract-wave authoring (15 modules, zero model calls) | 4 |
| 7 | T14 `jaunt reconcile` + batteries commit | serial |
| 8 | T15 docs + version bump 1.5.2 | serial |

After wave-6 commits, `jaunt check` exits 4 (unbuilt contracts) until T14 lands — expected mid-branch; waves 6→7 must not reorder. Waves 3-5 must follow T3 (the canary proves the pipeline before parallel spend).

---

### Task 1: Bug 1 — self-package eviction carve-out + registration preservation

**Files:** Modify `src/jaunt/registry.py` (`clear_registries` :133), `src/jaunt/discovery.py` (`evict_modules_for_import` :110-159 + new helpers), `src/jaunt/cli.py` (all 10 sites above). New `tests/test_self_hosting_registry.py`; extend existing eviction tests.

**Behavior — concrete signatures:**

```python
# src/jaunt/discovery.py
_SELF_PACKAGE: str = __package__ or "jaunt"   # top package of the RUNNING framework

def is_self_module(name: str) -> bool:
    """True for the running framework's own top package and its submodules."""

def self_preserved_modules(module_names: Iterable[str]) -> frozenset[str]:
    """Discovered names owned by the running framework that are ALREADY imported."""
    # {n for n in module_names if is_self_module(n) and n in sys.modules}

def evict_modules_for_import(*, module_names: list[str], roots: list[Path]) -> None:
    # signature unchanged; final delete loop skips is_self_module(name)
    # regardless of WHICH rule matched (exact, prefix, or __file__-under-roots)

def prepare_import_environment(*, module_names: list[str], roots: list[Path]) -> None:
    """clear_registries(preserve_modules=self_preserved_modules(module_names)),
    then evict_modules_for_import(...). The one shared entry point for CLI sites."""
```

```python
# src/jaunt/registry.py
def clear_registries(*, preserve_modules: frozenset[str] = frozenset()) -> None:
    """Clear all registries; keep entries whose .module is in preserve_modules
    (and _MODULE_MAGIC_REGISTRY keys in preserve_modules)."""
```

CLI sites: reorder so `discover_modules(...)` runs FIRST, then replace each clear+evict pair with `discovery.prepare_import_environment(module_names=modules, roots=...)`. Rationale (state in docstrings): the self-package is never evicted (evicting the running framework re-executes `jaunt/__init__.py` and forks the registry — the split-brain), so re-import of a cached self module is a no-op and its import-time registrations are unrecreatable; preservation scoped to **discovered ∩ imported ∩ self** keeps them WITHOUT leaking self-specs into adopter builds (adopter discovery never yields `jaunt.*` names → preserve set empty → clear stays total; the CLI snapshots `dict(registry.get_magic_registry())` immediately after `import_and_collect` at `cli.py:2185`, before any lazy self-import can pollute it). One mechanism covers magic, test, AND contract kinds (contract-wave modules are imported at CLI startup, so the contract path at `cli.py:724` needs this too).

**Tests (failing first):**
- `clear_registries(preserve_modules=...)` keeps exactly matching entries across all 4 registries; default arg = today's behavior byte-for-byte (existing suite's bare calls unaffected).
- Eviction carve-out: with roots covering jaunt's own source dir and `sys.modules["jaunt"]` live, `jaunt`/`jaunt.*` survive while a planted non-self module under the same roots is evicted.
- Split-brain regression pin: in-process `cli.main(["specs","--json","--root",<repo root>])` reports governed self modules (skip-if no root jaunt.toml, for wave ordering); pre-fix this returns zero specs.
- Leak pin: adopter fixture; pre-import `jaunt.heldout` in-process (simulating the tester path, `cli.py:3840`); run build discovery; assert no `jaunt.*` entries in the discovered spec snapshot.
- Second-pass pin: discovery twice in one process against a self-package-shaped fixture; second pass still sees all specs (preservation, not just carve-out).

**Known limitation (document in T15):** long-lived processes (`jaunt watch` / in-process daemon previews) won't pick up spec edits to already-imported framework modules until restart; one-shot CLI runs and daemon job subprocesses are always fresh.

Commit: `fix: never evict the running jaunt package during discovery; preserve self registrations across registry clears (self-hosting bug 1)`

---

### Task 2: Infra — root jaunt.toml, packaging, excludes, hooks, CI

**Files:** New `/jaunt.toml`; modify `.gitignore`, `.gitattributes`, `pyproject.toml`, `.githooks/pre-commit`, `.github/workflows/ci.yml`. New `tests/test_root_config.py` (smoke: `load_config` accepts the root jaunt.toml verbatim — strict config makes this a real test).

**Root jaunt.toml verbatim:**

```toml
version = 1

[agent]
engine = "codex"

[codex]
model = "gpt-5.5"
reasoning_effort = "high"
sandbox = "workspace-write"

[paths]
source_roots = ["src"]
test_roots = []

[build]
emit_stubs = true

[skills]
auto = false

[context]
repo_map = false

[contract]
battery_dir = "tests/contract"
derive = ["examples", "errors"]
strength = true

[semantic_gate]
enabled = true
model = "gpt-5.4-mini"
reasoning_effort = "high"
```

Justifications (record in commit body): `test_roots=[]` — jaunt's own suite stays plain pytest; default `["tests"]` would mass-import ~hundreds of pytest files as test-spec candidates. `[skills] auto=false` — self-specs import only stdlib + jaunt itself; auto-skills would spend codex calls for nothing (builtin skills stay default: free, lazy-loaded per the finding-19 probe). `[context] repo_map=false` — avoids adding a committed treedocs.yaml maintenance surface to this PR; contracts are self-contained by iron rule anyway. Multi-root gate passes trivially (single root).

**Other edits:**
- `.gitignore` — after line 8's `**/__generated__/` add:
  ```
  !src/jaunt/__generated__/
  !src/jaunt/__generated__/**
  ```
  (Negation pair: un-ignore the dir itself, then its contents; nested module dirs like `contract/` inside are not named `__generated__` so only the top dir needs re-inclusion. All generated files live under this ONE dir — 1.3 layout, `paths.py:8-16`.)
- `.gitattributes`: `JAUNT_LOG merge=union` (journal.py:131 does this lazily; make it explicit).
- `pyproject.toml`: add `src/jaunt/__generated__/**` to `[tool.ruff] exclude` and the ty exclude list (currently both only `examples/**`, :60-68). No `.pyi` excludes (stub freshness is AST-normalized — verified). Leave hatch config alone unless T3's wheel check fails (then `[tool.hatch.build.targets.wheel] artifacts = ["src/jaunt/__generated__/**"]` + sdist equivalent).
- `.githooks/pre-commit`: prepend a staged-`__generated__` guard adapted from mem-mcp-b's (`~/github.com/julep-ai/mem-mcp-b/.githooks/pre-commit:11-20`) but with the **1.3-layout spec mapping** (their dirname/dirname mapping breaks on nested modules): `spec="${gen/\/__generated__\//\/}"` — i.e. `src/jaunt/__generated__/contract/strength.py` → `src/jaunt/contract/strength.py`. Same regen-evidence escape (staged jaunt.toml/uv.lock/pyproject.toml).
- `.github/workflows/ci.yml` — new job (mirror the existing `test` job's uv setup steps; `needs: [lint]` because the ruff-autofix bot commits on top of every push, and check out the head ref):

```yaml
  jaunt-check:
    runs-on: ubuntu-latest
    needs: [lint]
    if: always() && (needs.lint.result == 'success' || needs.lint.result == 'skipped')
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event_name == 'pull_request' && github.head_ref || '' }}
      # ... same uv setup/sync steps as the test job ...
      - name: Jaunt check (spec <-> generated drift; deterministic, no API key)
        run: uv run jaunt check
```

CI is green immediately: a project with no magic specs and no contract drift exits 0.

**Tests:** root-config smoke; pre-commit guard verified by staging a lone fake generated file and expecting rejection (shell-level check in the task report is acceptable if no test precedent exists).

Commit: `chore: self-hosting infra — root jaunt.toml, committed __generated__ packaging, ruff/ty excludes, pre-commit guard, CI jaunt-check`

---

### Task 3: Canary conversion — guard.py (38 lines) + end-to-end pipeline proof

**Files:** Modify `src/jaunt/guard.py`; commit resulting `src/jaunt/__generated__/guard.py`, `src/jaunt/guard.pyi`, `JAUNT_LOG`.

**Behavior:** Follow the per-module conversion protocol (below). Govern `evaluate` (guard.py:17); keep `_owning_spec_hint` (:9) handwritten. Canary-only extras:
1. `uv run jaunt specs --json` — exactly one governed spec; `newly_governed` true only for `jaunt.guard:evaluate`.
2. Runtime rebind proof: `uv run python -c "import jaunt.guard, inspect; assert 'NotImplementedError' not in inspect.getsource(jaunt.guard.evaluate)"`.
3. Wheel proof: `uv build && unzip -l dist/*.whl | grep -E '__generated__/guard.py|guard.pyi'` — both present. If `__generated__` is missing from the wheel, add the hatch `artifacts` line from T2 and re-verify.
4. `uv run jaunt check` exit 0; clean tree after commit.

**If ANY canary step fails, STOP the workflow and report** — waves 3-5 spend real money on this pipeline.

Commit: `feat: self-host jaunt.guard — first jaunt-governed framework module`

---

### Tasks 4-9: Magic-wave conversions (waves 3-5, 2 parallel each)

All follow the per-module conversion protocol. Commit per task: `feat: self-host jaunt.<module> under jaunt.magic_module`.

| Task | Module | Size | Notes |
|------|--------|------|-------|
| T4 | src/jaunt/heldout.py | 291 L | Govern the pure report/redaction functions. ALL pytest hooks (:150-:226, undecorated real bodies or `@_hookimpl`-decorated) and module-level state stay handwritten. Runs inside adopter pytest subprocesses — the wheel-shipping story T3 proved is load-bearing here. |
| T5 | src/jaunt/migrate.py | 190 L | Public planning/apply entry points; cli.py wiring untouched. |
| T6 | src/jaunt/contract/strength.py | 247 L | `_skip_constant_ids` is imported by tests — it and any test-imported private helper stay handwritten; govern the public scoring functions. |
| T7 | src/jaunt/contract/cases.py | 354 L | Largest conversion; expect the priciest build. |
| T8 | src/jaunt/contract/drift.py | 62 L | `compute_drift_state` is the obvious governed symbol. |
| T9 | src/jaunt/contract/edits.py | 63 L | Small. |

#### Per-module conversion protocol (embed verbatim in each task prompt)

1. **Read first:** `jaunt-claude-plugin/skills/working-with-jaunt/SKILL.md` and `jaunt-claude-plugin/skills/build/SKILL.md`. Their iron rules and gate govern this task.
2. Read the module and every test file importing it. Governed set = public, behaviorally-specifiable functions. Anything imported by tests as a private helper, anything decorated, module state, and imports stay handwritten.
3. Author: `jaunt.magic_module(__name__)` directly after the imports. Replace each governed body with docstring contract + `raise NotImplementedError` (not `...` — ty rejects empty bodies under concrete return annotations; the forms are digest-equal). Docstrings must be SELF-CONTAINED: exact signatures preserved, invariants inlined, structured `Examples:`/`Raises:` blocks where behavior permits, mutable-state reads called out as read-at-call-time. Cross-module invariants go into `magic_module(prompt=...)`, never into references to sibling docstrings.
4. Classify before spending: `uv run jaunt status --json --progress none` — ONLY your module stale, class structural (first build); `newly_governed` exactly your intended symbols. Anything unexpected → fix the spec, don't build.
5. Build (single Bash command): `nohup uv run jaunt build --target jaunt.<module> --progress none > /tmp/jaunt-build-<slug>.log 2>&1 & tail --pid $! -f /dev/null`, then read the log fully.
6. **Surface advisories verbatim** in your task report — they are this PR's bug-hunting payload. Record the cost line.
7. Gate (all mandatory): `uv run jaunt check` exit 0 → FULL unchanged pytest suite → `uv run ruff check .` → `uv run ruff format .` → `uv run ty check` → line-review the generated body against the contract (first build: contract-silence divergence is the failure class no gate catches). Failures fix forward through the docstring only, then rebuild; NEVER touch `__generated__/**`, `.pyi`, or existing tests.
8. Commit with explicit paths: `git add src/jaunt/<path>.py src/jaunt/__generated__/<path>.py src/jaunt/<path>.pyi JAUNT_LOG && git commit ...` (retry once after 5s on index.lock).

---

### Tasks 10-13: Contract-wave authoring (wave 6, 4 parallel, zero model calls)

Docstring polish + `@jaunt.contract` on 1-4 load-bearing PUBLIC symbols per module. Committed code stays canonical (real bodies untouched; `@jaunt.contract` is a runtime no-op, `runtime.py:620-687`). Cascade modules are excluded by construction — none of the below executes during `import jaunt`.

- **T10:** digest.py, deps.py, header.py, module_api.py
- **T11:** module_contract.py, change_detection.py, validation.py, stub_emitter.py
- **T12:** cost.py, cache.py, config.py, parse_cache.py
- **T13:** discovery.py, generate/fingerprint.py (decorate the real logic here, NOT the 26-line `generation_fingerprint.py` compat wrapper), reconcile.py

Protocol: read both plugin skills; pick symbols a battery can pin without heavy fixtures (pure digest/parse/classify functions preferred — e.g. `module_digest`, `build_spec_graph`/`find_cycles`, `classify_change`, `stub_inputs_digest`, `load_config`); **prefer structured `Examples:`/`Raises:` blocks** — those derive deterministically at $0 (model fallback only for unstructured prose). Keep `Raises:` lines bare (known trailing-parenthetical gotcha from 1.0 silently drops the errors block). Gate = full pytest + ruff + ty. NOTE: `jaunt check` WILL exit 4 after these commits (unbuilt contracts) until T14 — state that in each commit body.

Commits: `feat: contract-mode coverage for <modules> (batteries derived in follow-up)` (one per task).

---

### Task 14: Reconcile — derive and commit contract batteries (wave 7, serial)

**Files:** run `uv run jaunt reconcile` (no `--target` exists — `cli.py:451`); commit `tests/contract/jaunt/**`, a `tests/contract/conftest.py` only if genuinely needed (prefer none — self-contained contracts), `JAUNT_LOG`.

**Behavior:** `nohup uv run jaunt reconcile > /tmp/jaunt-reconcile.log 2>&1 & tail --pid $! -f /dev/null`. Verify batteries at `tests/contract/jaunt/<module-path>/test_<qualname>.py` (`contract/runner.py:18-21`; collected automatically via pyproject `testpaths=["tests"]`). Report the deterministic-vs-model derivation split and strength scores. Then `uv run jaunt check` **exit 0** (un-reds CI from wave 6) + full gate. A failing battery means the contract prose overstated behavior — fix the DOCSTRING to match committed reality (contract mode: code is canonical) and re-reconcile; never hand-edit a derived region. Note: this wave exercises the wave-4/5 GENERATED contract machinery (strength/cases/drift/edits) for real — that is the dogfood payoff and also the riskiest integration point; treat misbehavior here as a possible generation defect in waves 4-5 and fix forward there.

Commit: `feat: committed contract batteries for 15 core modules (jaunt reconcile)`

---

### Task 15: Docs + version bump (wave 8, serial)

**Files:** `CLAUDE.md` (new "Self-hosting" section: which modules are jaunt-governed; committed `__generated__`/`.pyi`/`JAUNT_LOG`/battery artifacts; iron-rules pointer; fix-forward rule for framework specs; the T1 long-lived-process limitation; the cascade-exclusion list and the build-critical→contract-only rule), `README.md` ("jaunt builds itself" note — honest framing: which parts, and that `jaunt check` gates its own drift in CI), `pyproject.toml` version → `1.5.2` (**publish trigger — must ride this PR**). Load `.claude/skills/natural-writing/` before prose. Full gate green.

Commits: `docs: self-hosting — jaunt builds jaunt` then `chore: bump version to 1.5.2`

---

## Modules DROPPED from the maximal scope, with reasons

- **`src/jaunt/contract/derive.py` (264 L) — dropped from the magic wave.** Its rendered battery bytes are load-bearing for the deterministic `jaunt check` gate: a regenerated renderer differing in ANY emitted byte restales every committed battery — including the ones T14 creates — and sets up a self-referential churn loop (derive regenerates → batteries drift → reconcile rewrites → repeat). Also has a mid-module-import wrinkle. Stays fully handwritten; revisit post-1.6 if battery freshness ever keys on semantic rather than byte identity.
- **Cascade modules** (errors, runtime, module_magic, decorator_analysis, class_analysis, registry, spec_ref, paths): cannot bear jaunt decorators — they execute during `import jaunt` before the decorators are bound.
- Within retained modules, handwritten by rule: heldout's pytest hooks + module state, `strength._skip_constant_ids` and any test-imported private helper, `guard._owning_spec_hint`, all imports/signatures.

## Model-cost hotspots

| Spend | Count | Estimate |
|-------|-------|----------|
| gpt-5.5 first builds (magic wave) | 7 modules (guard 38 L → cases 354 L) | $3-10 each → $25-70 |
| Fix-forward rebuilds | expect 2-4 across the wave | $10-30 |
| Reconcile derivation | ~25-35 contract symbols; structured blocks derive at $0 | $5-20 |
| Semantic gate (gpt-5.4-mini) during docstring iteration | a handful | <$2 |
| **Total** | ~10 gpt-5.5 builds + 1 reconcile | **~$45-120; hard-stop at $150** |

## Post-workflow (main session)

codex gpt-5.5@high end-of-branch review (extra attention: T1 preservation semantics across all 10 CLI sites; wheel/sdist packaging of `__generated__`; every advisory surfaced in waves 2-5 — framework-dep advisories are release-note material and part of the 1.5.2 justification) → fix confirmed findings → PR → squash-merge → publish → FEEDBACK-REPLY/memory updates.
