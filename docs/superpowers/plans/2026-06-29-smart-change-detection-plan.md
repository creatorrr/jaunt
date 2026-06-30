# Smart change detection — Implementation plan

**Spec:** `docs/superpowers/specs/2026-06-29-smart-change-detection-design.md` (read it first).
**Branch:** `feat/smart-change-detection`.
**Execution:** dynamic Workflow of Opus subagents, each driving `codex exec`
(`-m gpt-5.5`, `model_reasoning_effort="medium"`, `--sandbox workspace-write`,
`approval_policy="never"`) to write the code.

## Hard rules for every agent

- Edit **only** the file(s) assigned to your task. Other files in the tree may have
  uncommitted edits from sibling tasks — **never** touch them.
- **Never** run `git checkout`/`reset`/`restore`/`stash`/`clean`/`commit`, and never
  tell codex to revert "out-of-scope" changes. The baseline is already committed;
  leave concurrent edits alone.
- Do **not** run the full test suite. Verify only your own file: it must parse
  (`python -c "import ast,sys; ast.parse(open(F).read())"`) and pass
  `uv run ruff check <F>`. The dedicated Verify phase runs pytest/ty.
- Drive codex non-interactively; if codex leaves the file syntactically broken or
  missing a required symbol, re-run codex with the specific error (max 3 attempts),
  then report what's left.
- Match surrounding code style. `from __future__ import annotations`, line length
  100, ruff E/F/I/UP/B.

## Frozen interface contracts (so parallel tasks agree)

### `digest.py` (Task DIGEST)
- `@dataclass(frozen=True) NormalizedContract`: fields `ref: str`, `kind: str`
  (`function|async_function|method|async_method|class`), `signature: str`,
  `decorator_meta: str`, `prose: str`, `body: str` (`""` for stubs),
  `members: str` (`""` for non-class).
- `normalized_contract(entry: SpecEntry) -> NormalizedContract`.
- `structural_digest(entry) -> str` — sha over everything in NormalizedContract
  **except** `prose`.
- `prose_digest(entry) -> str` — sha over `prose` only.
- `local_digest(entry) -> str` — sha over the **full** NormalizedContract (replaces
  the current raw-source hash; keep the same name/signature so callers are unchanged).
- `contract_snapshot(entry) -> dict` — JSON-able `{kind, signature, decorator_meta,
  prose}` for the sidecar.
- Reuse the existing AST extraction in `extract_source_segment` and mirror
  `class_analysis.split_class_members()` for whole-class specs (drop stub method
  bodies; keep `@jaunt.preserve`/non-stub bodies normalized, decorator stripped).
  Include symbol **kind** (async!) and the **name** explicitly. Use the effective
  decorated signature the build prompt uses (decorator_analysis), not raw AST args.

### `header.py` (Task HEADER)
- `format_header(...)` gains `spec_digests: dict[str, dict[str, str]] | None = None`
  (map `ref -> {"s": <structural sha>, "p": <prose sha>}`) and
  `digest_scheme: int = 2`. Emits `# jaunt:spec_digests={json}` and
  `# jaunt:digest_scheme=2` (digests normalized to `sha256:` like the others).
- `extract_spec_digests(source) -> dict | None`, `extract_digest_scheme(source) -> int | None`.
- Backward compatible: missing keys parse as `None`. `parse_header` still works.

### `config.py` (Task CONFIG)
- `@dataclass(frozen=True) SemanticGateConfig`: `enabled: bool = True`,
  `model: str = "gpt-5.4-mini"`, `reasoning_effort: str = "high"`.
- `JauntConfig.semantic_gate: SemanticGateConfig` (default factory).
- Parse `[semantic_gate]` in `load_config` with the existing `_as_*` validators;
  validate `reasoning_effort in {"low","medium","high"}`.

### `change_detection.py` (Task GATE — new file)
- `read_contract_sidecar(path: Path) -> dict[str, dict]` / `write_contract_sidecar(path, snapshots)`.
- `sidecar_path(module_file: Path) -> Path` — `<module_file>.contract.json`.
- `classify_change(old_snapshot: dict | None, entry: SpecEntry) -> str` →
  `"structural" | "prose" | "none"` (None/missing snapshot, or structural sub-digest
  differs → `structural`; only prose differs → `prose`; equal → `none`).
- `async gate_prose(*, old_prose, new_prose, signature, cfg, run_exec=run_codex_exec)
  -> str` → `"EQUIVALENT" | "MEANINGFUL"`. Builds the §6 prompt, calls the nano model
  read-only, parses the final message for an exact token; anything else/empty/error →
  `"MEANINGFUL"`. `run_exec` is injectable for tests (no real codex call under test).
- `async assess_specs(entries, old_snapshots, cfg, run_exec=...) -> dict[SpecRef, str]`
  → per-spec verdict `"MEANINGFUL" | "EQUIVALENT"` (structural ⇒ MEANINGFUL with no
  model call; prose ⇒ gate; none ⇒ EQUIVALENT).

### `builder.py` (Task BUILDER) — depends on all above
- `refreeze_module(*, package_dir, generated_dir, module_name, header_fields, snapshots)`:
  read existing file, strip old header, **validate the body** (reuse
  `validation.py`; run ty if `ty_retry_attempts` applies) — if invalid, raise/return a
  signal so the caller rebuilds instead — else rewrite header (new digests +
  `spec_digests` + `digest_scheme`) over the unchanged body atomically; rewrite sidecar.
- Build-flow integration (in `cli.py` build path, helper here): after
  `detect_stale_modules`, run the **migration escape hatch** first (scheme<2 + old raw
  digest matches ⇒ silent re-freeze, no gate), then classify each stale module's
  changed specs (per-spec, via `assess_specs`), roll verdicts up the dependency graph
  (MEANINGFUL spec ⇒ its module + transitive dependents rebuild; otherwise re-freeze).
- `write_generated_module` callers must now pass `spec_digests`/`digest_scheme` and
  write the sidecar so fresh builds seed the prior-contract state.

### `tester.py` (Task TESTER) — mirror of BUILDER for test modules
- `refreeze_test_module(...)` and the same gate/migration integration for the
  `detect_stale_test_modules` → regeneration path. Test modules carry `spec_digests`/
  `digest_scheme` + sidecar too.

### `cli.py` (Task CLI)
- `--no-semantic-gate` on `build` and `test` (force every normalized-digest change to
  rebuild; Layer A still applies). `--force` unchanged.
- Plumb `cfg.semantic_gate` into the build/test integration helpers.
- `build --json` / `test --json` gain `"refrozen": [...]`. `status` uses header
  `spec_digests` to label modules structural- vs prose-changed (no model call).

## Phases (workflow)

1. **Foundations** (parallel, disjoint files): DIGEST, HEADER, CONFIG, GATE.
2. **Integration** (sequential): BUILDER → TESTER → CLI.
3. **Tests** (parallel, disjoint test files): per §10 of the spec —
   `test_digest_normalization`, `test_change_detection`, `test_refreeze`,
   `test_migration`, `test_semantic_gate_cli`.
4. **Verify** (one agent): `uv run pytest`, `uv run ruff check .`, `uv run ty check`;
   drive codex gpt-5.5@medium to fix failures until green (bounded). Report status;
   do **not** commit (the main session commits after review).

## Acceptance criteria

- Ruff-reformatting/comment edits to a spec do not change `local_digest`; structural
  edits (async flip, default value, rename, class attr value, preserve body) do.
- Prose-only edits route through exactly one nano gate call; EQUIVALENT ⇒ validated
  re-freeze (body byte-identical, header/sidecar updated); MEANINGFUL ⇒ rebuild.
- Migration: scheme-1 fresh files re-freeze silently with no gate call.
- `--no-semantic-gate` keeps Layer A but rebuilds on any normalized change.
- `uv run pytest`, `uv run ruff check .`, `uv run ty check` all green.
