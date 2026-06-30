# Repo-Context Subsystem for Jaunt

**Date:** 2026-06-29
**Status:** Design — revised after Codex second-opinion review
**Branch:** `worktree-feat+repo-context`

## Summary

Give the Codex code-generation engine a high-level map of the project by
maintaining a tree of one-line descriptions for the repository and injecting it
into the build prompt, and make searching that repository faster and more
relevant via LightOn's `colgrep` (next-plaid). The tree is stored as a
version-controlled `treedocs.yaml` (treedocs format v0.2.0) maintained entirely
in Python so it is cross-platform and requires no external binary. `colgrep` is
opt-in: when present, Jaunt runs retrieval **before** `codex exec` and seeds the
results into the generation workspace, with graceful fallback when absent.

Delivered as one **repo-context subsystem**, but each piece is independently
toggleable behind config flags so failure modes stay isolated.

## Codex second-opinion: what changed (read this first)

A grounded Codex review (it read the actual source) caught real errors in the
first draft. Corrections folded into this revision:

1. **`codex exec` runs in a throwaway temp dir, not the repo.**
   `codex_backend.py:315-343` (`generate_module`) creates a
   `tempfile.TemporaryDirectory()`, writes the target file plus
   `_context/spec_*.py` / `_context/dep_*.pyi` (+ optional
   `_context/whole_class_contract.md`), then calls
   `run_codex_exec(cwd=<tempdir>)`. **Consequence:** the agent cannot run
   `colgrep` against the real repo. Retrieval is therefore **pre-computed by
   Jaunt and written into `_context/relevant_*.py`**, the same channel deps use.
2. **Two cache layers must include any new prompt context** or builds reuse
   stale output:
   - the response cache key (`cache.py:33` `cache_key_from_context`), and
   - the generated-file freshness digest (`builder.py:435`
     `_build_context_digest`).
3. **Staleness needs per-file source content digests.** The treedocs
   `signature` covers descriptions only, and `ParseCache`
   (`parse_cache.py:33`) is keyed by `sha256(path)` + Python tag and validated
   by `mtime_ns`/size — **not** content. Neither can detect "the file changed,
   its 1-line description is now stale." We store a per-entry source content
   digest in a sidecar cache.
4. **Prompt-cache hygiene:** volatile text (`last_updated`, the signature,
   retrieval scores, nondeterministic ordering) is kept **out** of the prompt
   body so it does not bust Codex's input-token cache on every build. Static
   content goes in a stable prefix; the only volatile block (retrieval) goes
   last.
5. **Atomic writes + a lock** on `treedocs.yaml` (build and watch can overlap;
   a failed build must not leave doc churn as its main side effect).
6. Factual fixes: the existing package tree scans the **module's own
   directory** (`Path(entries[0].source_file).parent.rglob("*.py")`,
   `builder.py:659`), not the whole repo; `_cap_skill_body`
   (`skill_manager.py:151`) elides long fenced code then hard-truncates (it does
   not "drop deepest lines first"); CLI parser registration and dispatch live in
   a large `_build_parser()` + `main()` (`cli.py`), not a tidy `118-260` range.

**Posture decisions (user-confirmed, overriding Codex's more conservative
advice):** repo-map defaults **ON** (AST-only); enrichment is **build-time**
hybrid with AST fallback. Codex recommended default-off and manual enrichment;
the user chose to keep the original aggressive posture. We mitigate Codex's
nondeterminism/cost concerns by caching descriptions per source-content digest
(identical repo state ⇒ identical prompt) and by the cache-layer + prompt-cache
work above.

## Goals

- Maintain a durable, reviewable, incrementally-updated tree of 1-line
  descriptions of the repo's directories and Python source files.
- Inject that tree into the Codex build prompt so generation has a project-wide
  map, alongside the existing per-module package tree and skills block — without
  breaking the response cache or generated-file freshness.
- Keep the default build **offline and free** on the always-on path: AST + YAML
  only, no API calls, no external binaries.
- Optionally enrich descriptions with a cheap batched LLM pass (build-time,
  cached, AST fallback).
- Instrument `colgrep` for faster semantic retrieval, pre-computed by Jaunt and
  seeded into the generation workspace, when the binary is installed and the
  feature is enabled.

## Non-Goals

- Depending on the macOS-only `treedocs` Swift binary (we adopt the *format*).
- Making `colgrep` or LLM enrichment a hard dependency.
- Having the Codex agent shell out to `colgrep` (impossible: temp-dir cwd).
- Mutating the user's global Codex config (e.g. `colgrep --install-codex`).
- Documenting non-Python files or descending into `__generated__`/cache dirs.
- Per-directory nested `treedocs.yaml` files (documented as the v2 path).

## Key Decisions

| Decision | Choice | Note |
|----------|--------|------|
| Scope | One subsystem, independently flag-toggleable | isolates failure modes (Codex) |
| Description generation | Hybrid: AST baseline (default) + build-time LLM enrichment | enrichment cached per source digest |
| treedocs | Adopt `treedocs.yaml` v0.2.0; maintain in Python | single root file for v1 |
| colgrep | Pre-computed retrieval → `_context/relevant_*.py`; opt-in | agent-run mode dropped (temp dir) |
| Repo-map default | **On** (AST-only) | user-confirmed over Codex's default-off |
| Enrichment | **Build-time** hybrid + AST fallback | user-confirmed; bounded by digest cache |
| Granularity | Directories + Python source files only | skip non-`.py`, generated, cache |
| Staleness | Per-entry **source content digest** in a sidecar | treedocs signature can't detect it |
| Caches | Extend response-cache key **and** freshness digest | + regression tests |

## Background: how Jaunt assembles the Codex prompt today

- **Generation workspace:** `codex_backend.py:315-343` — temp dir; target file +
  `_context/spec_*.py` + `_context/dep_*.pyi` (+ `_context/whole_class_contract.md`);
  `run_codex_exec(cwd=<tempdir>)`. Anything Codex should "see" must be a file in
  that temp dir or text in the prompt.
- **Prompt assembly:** `codex_backend.py:_build_prompt()` (~371-402) concatenates
  ordered blocks from a `ModuleSpecContext`: preamble → whole-class instruction →
  `build_instructions_block` → `module_contract_block` → `base_contract_block` →
  `package_context_block` → `skills_block` → footer → optional retry context.
- **`ModuleSpecContext`:** `generate/base.py:16-38` — block carrier. Populated in
  `builder.py` `_component_payload()` (~1287-1311).
- **Response cache key:** `cache.py:33` `cache_key_from_context()` hashes
  provider/model/`kind`/`spec_module`/`generated_module`/`expected_names`/
  `spec_sources`/… New context that affects generation MUST be added here.
- **Generated freshness:** `builder.py:435` `_build_context_digest()` hashes a
  fixed tuple of blocks (`module_contract`, `base_contract`, `blueprint_source`,
  `build_instructions`, `attached_test_specs`, `package_context`). `status`
  computes this separately from `build`; both must agree.
- **Skills injection (analog):** `skills_auto.py:ensure_pypi_skills_and_block()`
  (~59-109) + `skill_manager.py:build_skills_block()` (~198-256), capped by
  `_cap_skill_body()` (`skill_manager.py:151`, default 8000): elides long fenced
  examples, then hard-truncates with a marker.
- **Package tree:** `builder.py:_build_package_context_block()` (~647-705) scans
  `Path(entries[0].source_file).parent.rglob("*.py")` — the **module's
  directory**, excluding generated dir and `__pycache__`.
- **Discovery:** `discovery.py:discover_module_files()` (~126-184) enumerates
  `(module_name, path)` under source roots; `_module_name_for_file()` (~103-123)
  maps paths to dotted names. Note: multiple `source_roots` are supported, which
  the tree must handle (root-prefix semantics).
- **Digests / cache:** `digest.py` `local_digest` (per-`SpecEntry` source segment
  + decorator kwargs), `module_digest` (spec-graph digests). There is **no**
  generic source-file content digest today — the subsystem adds one.
  `parse_cache.py:ParseCache` keyed by `sha256(path)` + py tag, validated by
  mtime/size (memo fast path checks size only).
- **CLI:** `cli.py` argparse; subcommands in `_build_parser()`, dispatch in
  `main()`. `watcher.py:build_cycle_runner()` (~127) builds its own
  build/test namespaces — new flags (`--no-repo-map`) must be plumbed there too.
- **Config:** `config.py` `tomllib`; optional sections → frozen dataclasses via
  `_as_*` helpers (`SkillsConfig` ~84-88).

## Architecture

```
src/jaunt/
  repo_context/
    __init__.py
    tree.py          # treedocs.yaml model: load, walk/diff (sync), atomic write+lock, signature
    digests.py       # per-file source content digest + sidecar cache (.jaunt/tree-cache.json)
    describe.py      # AST baseline + build-time LLM enrichment (cached per source digest)
    block.py         # render capped repo-map block + package-tree annotation (prompt-cache safe)
    search.py        # colgrep: detect, ensure index, query --json, write _context/relevant_*.py, fallback
  config.py          # + ContextConfig / ContextSearchConfig
  cli.py             # + `jaunt tree` (+ --check, --enrich, --no-repo-map) and dispatch
  cache.py           # cache_key_from_context() extended with new context fields
  builder.py         # tree.sync() + retrieval; populate new ModuleSpecContext fields; _build_context_digest() extended
  generate/
    base.py          # + repo_map_block, relevant_context_files, search done via _context
    codex_backend.py # write _context/relevant_*.py; append repo_map_block; relevant pointer last
  watcher.py         # plumb --no-repo-map / repo_map into build_cycle_runner
```

### Unit responsibilities

- **`tree.py`** — owns `treedocs.yaml`. `load(path)`, `sync(roots, exclude)`
  (add new, drop ghosts, mark stale via `digests`), atomic `write(path)` (temp
  file + `os.replace`, guarded by a file lock), `signature()` (over descriptions,
  for manual-edit drift), `is_drifted()`.
- **`digests.py`** — `source_digest(path) -> str` (sha256 of file content) and a
  sidecar store mapping path → `{source_digest, description, enriched: bool}` in
  `.jaunt/tree-cache.json`. This is what actually answers "is this description
  stale?" and caches enrichment.
- **`describe.py`** — `ast_describe(path) -> str` (deterministic);
  `enrich(stale_entries, backend) -> dict[path,str]` (batched, optional, cached,
  AST fallback).
- **`block.py`** — `render_repo_map(treedoc, max_chars)` (stable ordering, no
  volatile fields), `annotate_package_tree(text, treedoc)`. Pure given a tree.
- **`search.py`** — `available()`, `ensure_index(root)`, `query(text, max_hits)
  -> list[Hit]`, `write_context_files(hits, ctx_dir)`, `fallback_query(...)`.
  All `subprocess`; every failure ⇒ "no hits", never fatal.

## The `treedocs.yaml` artifact

Conforms to **treedocs schema v0.2.0**. Lives at project root, committed.
Top-level: `schema_version: "0.2.0"`, `project{name,version,last_updated}`,
`signature: sha256:<64hex>`, `tree`. File entries use the compact string form;
directories use `_doc`.

```yaml
# yaml-language-server: $schema=https://dandylyons.github.io/treedocs/schemas/0.2.0/treedocs.schema.json
schema_version: "0.2.0"
project:
  name: "jaunt"
  version: "1.0.0"
  last_updated: "2026-06-29"
signature: "sha256:0000000000000000000000000000000000000000000000000000000000000000"
tree:
  "src":
    "_doc": "Library source"
    "jaunt":
      "_doc": "Jaunt package: spec-driven codegen CLI + runtime"
      "cli.py": "CLI entry point (build, test, init, clean, status, watch, tree)"
      "builder.py": "Build orchestration and parallel scheduling"
```

### Two kinds of "freshness" (kept separate)

- **Manual-edit drift** — the treedocs `signature` (sha256 over canonical tree
  *descriptions*, excluding `signature`/`last_updated`). Detects a human editing
  the YAML by hand. Gated by `jaunt tree --check`.
- **Source drift** — per-entry **source content digest** stored in
  `.jaunt/tree-cache.json` (gitignored). `sync()` compares each file's current
  content digest to the cached one to decide which descriptions to regenerate.
  This is the one the signature alone cannot do. `--check` also fails if any
  tracked file's source digest differs from its cached digest (stale
  description) or filesystem paths ≠ tree paths (ghost/missing).

### Multiple source roots & single file

For multiple `source_roots`, the tree namespaces each root at the top level so
paths are unambiguous. v1 uses a **single root `treedocs.yaml`**. Codex's
dissent (per-directory files reduce merge conflicts and unrelated rebuild
invalidation for large repos) is acknowledged; per-directory nesting is the
documented **v2** path (treedocs supports it natively).

### Atomic write + lock

`write()` serializes to a temp file in the same directory and `os.replace()`s it
into place, holding a lock file (e.g. `.jaunt/tree.lock`) for the read-modify-
write so overlapping build/watch runs cannot corrupt or interleave. A failed or
cancelled build must not commit partial doc churn.

## Description generation (hybrid)

### AST baseline — default, free, offline, always-on

Per Python file: (1) module docstring first non-empty line; else (2) synthesize
from public `def`/`class` names + `@jaunt.magic`/`@jaunt.test`/`@jaunt.contract`
decorators; else (3) `"Python module"`. Directories: `__init__.py` docstring
first line, else a child rollup. Parsing reuses `ParseCache`.

### Build-time LLM enrichment — opt-in (`[context] enrich = true`)

When enabled and a backend is available, during `sync()`:
- Select entries whose **source content digest changed** since the sidecar's
  cached digest (so unchanged files are never re-enriched).
- Issue **one batched** request (path + AST summary + short head per item) → JSON
  `path -> one-line description`. Validate/trim to one capped line.
- On any failure (no backend, bad JSON, timeout) ⇒ **AST baseline** for that
  entry. Never errors the build.
- Cache `{source_digest, description, enriched:true}` in the sidecar.

**Determinism guard (mitigates Codex):** the prompt only ever embeds the
*cached* description for a given file content. Identical repo state ⇒ identical
descriptions ⇒ identical prompt ⇒ no response-cache / prompt-cache churn.
Enrichment changes a description only when the file's content already changed
(which already triggers a rebuild).

## Injection into the Codex prompt

`codex_backend.generate_module()` already seeds `_context/`. Additions:

1. **`repo_map_block`** (new `ModuleSpecContext` field) — whole-repo 1-line map
   from `treedocs.yaml`, capped to `[context] max_chars` (default 6000), **stable
   ordering, no `last_updated`/signature/scores in the text**. Appended in
   `_build_prompt()` after `package_context_block`, near `skills_block`. Header
   `## Repository map`.
2. **Annotated package tree** — `_build_package_context_block()`'s lines get
   their descriptions appended (fallback: bare path). This mutates the existing
   `package_context_block`, which is already in both cache layers.
3. **Retrieval seeded as files** — when colgrep is enabled, Jaunt writes the top
   hits into `_context/relevant_0.py …` in the temp workspace and adds a short
   pointer line to the prompt ("Read `_context/relevant_*.py` for related code in
   the repo."), placed **last** (it is the only volatile block). The agent reads
   files; it never runs colgrep.

### Cache integration (mandatory, with tests)

- `cache.py:cache_key_from_context()` gains: `repo_map_block`, the rendered
  retrieval context (hash of the `_context/relevant_*.py` contents), and the
  (already-included) `package_context_block` now carrying annotations.
- `builder.py:_build_context_digest()` gains the same new blocks so generated-
  file freshness and `status`/`build` agree.
- Regression tests assert: changing the repo map or retrieval changes both keys;
  unchanged state leaves them stable.

All blocks are empty when their feature is off ⇒ byte-identical prompts and keys
for users who disable the subsystem (no forced cache invalidation on upgrade).

## colgrep integration (opt-in, pre-computed retrieval)

Gated by `[context.search] enabled = true` **and** `shutil.which("colgrep")`.

- **Index freshness:** at `build`/`watch` start, `colgrep init` (idempotent);
  colgrep's own `state.json` handles incremental re-index. Index lives in
  colgrep's per-user data dir, not the repo.
- **Retrieval:** per spec, `colgrep --json "<docstring + expected_names>"`, take
  up to `[context.search] max_hits` (default 8), **deterministic tie-break**
  (sort by score then path), write capped snippets into
  `_context/relevant_*.py`, hash that rendered content into the cache key +
  freshness digest.
- **Fallback:** colgrep absent/disabled/error/timeout ⇒ no `_context/relevant_*`
  files, empty pointer; optional ripgrep-over-expected-names fallback.
- **No global Codex config mutation.** We do not run `colgrep --install-codex`
  (documented as an optional user convenience only).

## Configuration

```toml
[context]
repo_map = true            # maintain treedocs.yaml + inject the map (AST-only by default)
repo_map_file = "treedocs.yaml"
enrich = false             # opt-in build-time LLM enrichment
max_chars = 6000           # cap for the injected repo-map block

[context.search]           # colgrep
enabled = false            # opt-in; requires the colgrep binary
internal_retrieval = true
max_hits = 8
```

Frozen dataclasses `ContextConfig` (nested `ContextSearchConfig`), parsed via
`_as_*`. `inject_agent_instructions` from the first draft is removed (agent-run
colgrep is impossible). Defaults: repo-map on (AST-only), enrichment off,
colgrep off.

`pyyaml` is added as a base runtime dependency (the format requires real YAML
round-tripping) but is **lazy-imported** inside `repo_context/` so it loads only
when the repo-map path actually runs. (Codex flagged growing the 5-package base
set; lazy import contains the cost and keeps `clean`/`status --json` import-light.)

## CLI

- `jaunt tree` — sync + regenerate `treedocs.yaml`. `--force` (regen all),
  `--enrich`/`--no-enrich` (override config), `--json`, common flags.
- `jaunt tree --check` — deterministic drift gate: ghost/missing paths,
  signature mismatch (manual edit), or any tracked file whose source digest ≠
  cached digest (stale description). No model. **Exit 4** on drift (matches
  `check`), 0 clean.
- `build`/`test`: run `tree.sync()` before prompt assembly when `repo_map` on;
  ensure colgrep index when search enabled. `--no-repo-map` opt-out, plumbed
  through `cli.py` **and** `watcher.build_cycle_runner()`.
- `status` (+ `--json`): report tree drift counts (new/ghost/stale).

## Data flow (build)

```
jaunt build
  └─ config: repo_map on?
       ├─ tree.sync(roots): walk fs; diff vs treedocs.yaml + sidecar digests
       │     ├─ add new .py/dir, drop ghosts
       │     ├─ source digest changed? → describe.ast_describe()  (always)
       │     └─ enrich on? → describe.enrich(changed, backend)    (batched, cached, fallback)
       ├─ tree.write(treedocs.yaml)  (atomic + lock; recompute signature)
       └─ search enabled? → search.ensure_index(root)
  └─ per module → _component_payload():
       ├─ block.render_repo_map(treedoc, max_chars)         → repo_map_block        (stable text)
       ├─ block.annotate_package_tree(...)                  → package_context_block
       └─ search.query(spec) → write _context/relevant_*.py → prompt pointer (last) (volatile, hashed)
  └─ cache_key_from_context() + _build_context_digest() include the new blocks
  └─ codex_backend.generate_module(): seeds _context/, _build_prompt(), codex exec
```

## Error handling & graceful degradation

- Missing/corrupt `treedocs.yaml` ⇒ rebuild from empty, log once.
- Missing/corrupt sidecar ⇒ treat all descriptions stale (full re-describe).
- Enrichment failure ⇒ AST baseline; build proceeds.
- colgrep absent/disabled/error/timeout ⇒ no retrieval files; build proceeds.
- Atomic write + lock prevents partial/corrupt YAML under overlapping runs.
- `jaunt tree --check` is the only fatal drift path (exit 4), only when invoked.

## Testing strategy

- **Unit (offline, deterministic):** `ast_describe()` over fixtures;
  `tree.sync()` add/ghost/stale via sidecar digests; `signature()` stability;
  `render_repo_map()` capping + stable ordering; config parsing + defaults; CLI
  `tree`/`--check` exit codes.
- **Cache regression (critical):** adding/changing `repo_map_block` or retrieval
  changes both `cache_key_from_context()` and `_build_context_digest()`;
  unchanged state keeps them byte-stable; feature-off ⇒ keys identical to today.
- **Mocked backend:** `enrich()` success + fallback-on-raise; digest-cache hit
  skips re-enrichment.
- **colgrep (mocked, no real binary/network):** present→parse JSON; absent→
  fallback/empty; timeout; malformed JSON; deterministic tie ordering; `_context/
  relevant_*.py` written + hashed into keys.
- **Prompt assembly:** `_build_prompt()` includes/excludes each block per flag;
  retrieval pointer is last.

Suite stays offline and key-free.

## Landing plan (one subsystem, isolated phases)

Repo-map is default-on, so phase 1 includes injection **and** the cache work
(they cannot land apart safely).

1. **Repo-map core + injection + cache integrity:** `tree.py` (atomic write +
   lock), `digests.py` (sidecar), `describe.ast_describe()`, `block.py`,
   `ContextConfig`, `jaunt tree` + `--check`, `repo_map_block` injection +
   package-tree annotation, **extend `cache_key_from_context()` +
   `_build_context_digest()` + status/build parity + regression tests**, pyyaml
   (lazy). Default on, offline.
2. **Build-time enrichment:** `describe.enrich()` + `[context] enrich`, batched,
   digest-cached, AST fallback.
3. **colgrep retrieval (opt-in):** `search.py`, `[context.search]`, pre-run
   retrieval → `_context/relevant_*.py` + prompt pointer (hashed into keys),
   index freshness, fallbacks, mocked tests.

## Open questions / risks

- **Token budget:** repo map + skills + package context + retrieval runs once per
  stale module, in parallel. `max_chars` caps the map; retrieval is `max_hits`-
  bounded. Revisit caps after measuring real prompt sizes.
- **Single root file churn:** acknowledged; per-directory is the v2 mitigation.
- **Enrichment first-run cost:** one larger batched call on first enrichment of a
  big repo; digest-caching makes subsequent runs cheap. `--no-enrich` + default-
  off mitigate.
- **treedocs schema evolution:** pinned to v0.2.0; a bump is a follow-up.

## References

- next-plaid / colgrep: https://github.com/lightonai/next-plaid
- colgrep CLI README: https://github.com/lightonai/next-plaid/blob/main/colgrep/README.md
- treedocs: https://github.com/DandyLyons/treedocs
- treedocs schema v0.2.0: https://dandylyons.github.io/treedocs/schemas/0.2.0/treedocs.schema.json
