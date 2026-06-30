# Repo-Context Subsystem for Jaunt

**Date:** 2026-06-29
**Status:** Design — pending approval
**Branch:** `worktree-feat+repo-context`

## Summary

Give the Codex code-generation engine a high-level map of the project by
maintaining a tree of one-line descriptions for the repository and injecting it
into the build prompt, and make searching that repository faster and more
relevant via LightOn's `colgrep` (next-plaid). The tree is stored as a
version-controlled `treedocs.yaml` (treedocs format v0.2.0) maintained entirely
in Python so it is cross-platform and requires no external binary. `colgrep` is
opt-in: when present it powers both internal context retrieval and an
agent-facing search affordance, with graceful fallback when absent.

This is delivered as one integrated **repo-context subsystem**, but every piece
lands behind config flags so it can ship incrementally.

## Goals

- Maintain a durable, reviewable, incrementally-updated tree of 1-line
  descriptions of the repo's directories and Python source files.
- Inject that tree into the Codex build prompt so generation has a project-wide
  map, alongside the existing per-module package tree and skills block.
- Keep the default install **offline and free**: the always-on path is pure
  Python (AST + YAML), no API calls, no external binaries.
- Optionally enrich descriptions with a cheap batched LLM pass.
- Instrument `colgrep` for faster, semantic search — both as internal retrieval
  for prompt context and as an affordance the Codex agent can call — when the
  binary is installed and the feature is enabled.

## Non-Goals

- Depending on the macOS-only `treedocs` Swift binary (we adopt the *format*,
  not the tool).
- Making `colgrep` or LLM enrichment a hard dependency. Both are opt-in with
  fallbacks.
- Mutating the user's global Codex configuration by default (e.g. via
  `colgrep --install-codex`). We surface colgrep to the agent through Jaunt's
  own prompt, which Jaunt controls.
- Documenting non-Python files or descending into `__generated__`/cache dirs.

## Key Decisions (locked during brainstorming)

| Decision | Choice |
|----------|--------|
| Scope | All three asks as one integrated subsystem, flag-gated for incremental landing |
| Description generation | Hybrid: AST baseline (default) + optional LLM enrichment |
| treedocs | Adopt `treedocs.yaml` format v0.2.0; maintain it in Python |
| colgrep | Both modes — internal `--json` retrieval + agent-facing affordance |
| Repo-map default | **On by default**, AST-only (free/offline) |
| Granularity | Directories + Python source files only (skip non-`.py`, generated, cache) |

## Background: how Jaunt assembles the Codex prompt today

(From a codebase survey; cited so the implementation plan targets the right
seams.)

- **Prompt assembly:** `src/jaunt/generate/codex_backend.py:_build_prompt()`
  (~lines 371–402) concatenates ordered blocks pulled from a
  `ModuleSpecContext`. The order today is, roughly: target/exports preamble →
  contract instructions → `build_instructions_block` → `module_contract_block`
  → `base_contract_block` → `package_context_block` → `skills_block` →
  "edit only the target file" footer → optional retry error context.
- **`ModuleSpecContext`:** `src/jaunt/generate/base.py:16–38` — the dataclass
  holding every block. Populated in `src/jaunt/builder.py` `_component_payload()`
  (~lines 1287–1311).
- **Closest analog — skills injection:** `src/jaunt/skills_auto.py`
  `ensure_pypi_skills_and_block()` (~59–109) discovers PyPI dists, generates
  skills under `.agents/skills/<dist>/SKILL.md`, and
  `src/jaunt/skill_manager.py:build_skills_block()` (~198–256) renders a
  size-capped block (`_cap_skill_body()`, cap = `[skills] max_chars_per_skill`,
  default 8000). The repo-map block mirrors this shape.
- **Existing package tree:** `src/jaunt/builder.py:_build_package_context_block()`
  (~647–705) already scans `package_root.rglob("*.py")` (excluding the generated
  dir and `__pycache__`) and renders a `## Package tree`. We annotate its lines
  with descriptions.
- **Discovery:** `src/jaunt/discovery.py:discover_module_files()` (~126–184)
  enumerates `(module_name, path)` under source roots, excluding generated dir,
  exclude globs, and `__pycache__`. `_module_name_for_file()` (~103–123) maps
  paths to dotted module names.
- **Digests / cache:** `src/jaunt/digest.py` (`local_digest`, `graph_digest`,
  `module_digest`) and `src/jaunt/parse_cache.py` (`ParseCache`, keyed by file
  hash + Python version, validated by mtime/size). The repo-map reuses these for
  staleness and for the treedocs `signature`.
- **Config:** `src/jaunt/config.py` uses `tomllib`; optional sections parse via
  `_as_*` helpers into frozen dataclasses (e.g. `SkillsConfig` at ~84–88, parsed
  ~385–402). New config follows this pattern.
- **CLI:** `src/jaunt/cli.py` uses `argparse`; subcommands registered in
  `_build_parser()` (~118–260) with a dispatch in `main`. Common flags via
  `_add_common_flags`.

## Architecture

A new module owns tree maintenance; small, well-bounded helpers own colgrep and
config; injection extends the existing prompt-assembly seams rather than
replacing them.

```
src/jaunt/
  repo_context/
    __init__.py
    tree.py          # treedocs.yaml model: load, walk/diff (sync), write, signature
    describe.py      # description generation: AST baseline + optional LLM enrichment
    block.py         # render the capped repo-map block + per-module annotations
    search.py        # colgrep detection, index freshness, --json retrieval, fallback
  config.py          # + ContextConfig / ContextSearchConfig dataclasses + parsing
  cli.py             # + `jaunt tree` subcommand and dispatch
  builder.py         # call tree.sync() + retrieval; populate new ModuleSpecContext fields
  generate/
    base.py          # + repo_map_block, relevant_code_block, search_hint_block fields
    codex_backend.py # append new blocks in _build_prompt()
```

### Unit responsibilities

- **`tree.py`** — *What:* owns the on-disk `treedocs.yaml` and its sync.
  *Interface:* `load(path) -> TreeDoc`, `TreeDoc.sync(roots, exclude) -> SyncResult`
  (adds new paths, drops ghost paths, marks stale entries by content digest),
  `TreeDoc.write(path)`, `TreeDoc.signature() -> str`, `TreeDoc.is_drifted()`.
  *Depends on:* `discovery`, `digest`, `paths`, `pyyaml`.
- **`describe.py`** — *What:* produces a 1-line description for a file or dir.
  *Interface:* `ast_describe(path) -> str` (deterministic), `enrich(entries,
  backend) -> dict[path,str]` (batched, optional). *Depends on:* `parse_cache`,
  the generator backend (only when enriching).
- **`block.py`** — *What:* renders prompt text. *Interface:*
  `render_repo_map(treedoc, max_chars) -> str`,
  `annotate_package_tree(tree_text, treedoc) -> str`,
  `render_search_hint() -> str`. *Depends on:* `tree.py` only.
- **`search.py`** — *What:* all colgrep interaction. *Interface:*
  `available() -> bool`, `ensure_index(root)`, `query(text, max_hits) ->
  list[Hit]`, `fallback_query(...)`. *Depends on:* `subprocess`, `shutil.which`,
  ripgrep fallback.

Each unit is independently testable: `tree`/`describe`/`block` are pure given a
filesystem and (for enrich) a mocked backend; `search` is mocked or skipped when
the binary is absent.

## The `treedocs.yaml` artifact

Conforms to **treedocs schema v0.2.0**
(`https://dandylyons.github.io/treedocs/schemas/0.2.0/treedocs.schema.json`).
Lives at the project root, committed to git.

Required top-level keys: `schema_version` (`"0.2.0"`), `project`
(`name`, `version`, `last_updated` as `YYYY-MM-DD`), `signature`
(`sha256:<64 hex>`), `tree`. Tree entries are either a **compact string**
(the description) or a **structured object** with `_description`, `_doc`
(directory docs), `_references`, `_link`, plus child keys for nesting.

Jaunt writes the compact string form for files and uses `_doc` for directories,
keeping the file lean and human-reviewable. Example:

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

### Signature and drift

The `signature` is `sha256:` over a canonical serialization of the `tree`
(stable key ordering, descriptions only — excluding the signature and
`last_updated` themselves). Computed via the existing `digest` style hashing.
`is_drifted()` = recomputed signature ≠ stored signature, **or** the set of
real filesystem paths ≠ the set of tree paths (ghost/missing entries). This is
what `jaunt tree --check` gates on — deterministic, no model.

> Note: treedocs allows a child folder to own its own nested `treedocs.yaml`.
> Jaunt v1 uses a **single root `treedocs.yaml`** for the whole project and does
> not split per-directory. (Forward-compatible: we can add nested files later.)

## Description generation (hybrid)

### AST baseline — default, free, offline, always-on

For each Python file, derive a 1-line description deterministically:
1. If the module docstring exists, use its first non-empty line (trimmed,
   single line, length-capped).
2. Else synthesize from the public surface: top-level `def`/`class` names and
   any `@jaunt.magic` / `@jaunt.test` / `@jaunt.contract` decorators
   (e.g. `"magic specs: TokenStore, verify_jwt"`).
3. Else a minimal fallback (`"Python module"`).

For directories: use the package `__init__.py` docstring first line if present,
else a rollup naming a few notable children.

Parsing reuses `parse_cache` so ASTs are cached by content hash.

### LLM enrichment — opt-in (`[context] enrich = true`)

When enabled **and** a generator backend is available:
- Collect entries whose content digest changed since their cached description.
- Issue **one batched** request: each item carries its path, AST summary, and a
  short head of the file; the model returns a JSON map `path -> one-line
  description`. (Batched to avoid one `codex exec` per file.)
- Validate/trim each result to a single capped line; on any failure or absent
  backend, **fall back to the AST baseline** for that entry.
- Cache each enriched description keyed by content digest in the parse-cache
  directory, so unchanged files are never re-enriched.

Enrichment never blocks a build: failures degrade to baseline, never error out.

## Injection into the Codex prompt

Three new optional blocks on `ModuleSpecContext` (`generate/base.py`), appended
in `codex_backend.py:_build_prompt()` in this order, **after**
`package_context_block` and near `skills_block`:

1. **`repo_map_block`** — a whole-repo 1-line map rendered from `treedocs.yaml`,
   size-capped to `[context] max_chars` (default 6000) using the same capping
   strategy as `_cap_skill_body` (drop deepest/least-relevant lines first, mark
   truncation). Header: `## Repository map`.
2. **`relevant_code_block`** — (colgrep internal retrieval, see below) top-k
   semantically relevant existing code units for this spec, capped. Header:
   `## Relevant existing code`. Empty when retrieval is disabled/unavailable.
3. **`search_hint_block`** — (colgrep agent affordance) a short instruction:
   colgrep is available; run `colgrep --json "<intent>"` to find relevant code.
   Empty when colgrep is disabled/unavailable.

Additionally, `_build_package_context_block()`'s `## Package tree` lines are
annotated with their descriptions from `treedocs.yaml` (falling back to the
bare path when no description exists).

All blocks are omitted (empty string) when their feature is off, so prompts are
unchanged for users who disable the subsystem.

## colgrep integration (opt-in, both modes)

Gated by `[context.search] enabled = true` **and** `shutil.which("colgrep")`.

- **Index freshness:** at the start of `build`/`watch`, if enabled, call
  `colgrep init` once (idempotent) and rely on colgrep's own incremental
  re-indexing (`state.json`) for changed files. Index lives in colgrep's
  per-user data dir (`~/.local/share/colgrep/indices/...` on Linux) — not in the
  repo.
- **Internal retrieval (Mode B):** for each spec, query
  `colgrep --json "<docstring + expected_names>"`, take up to
  `[context.search] max_hits` (default 8) hits, and render them into
  `relevant_code_block`. **Fallback:** when colgrep is absent/disabled, optional
  ripgrep-over-expected-names / AST-dep selection produces a smaller block (or
  the block is empty).
- **Agent affordance (Mode A):** when
  `[context.search] inject_agent_instructions = true`, add `search_hint_block`
  so the Codex agent can call colgrep mid-generation. This is **scoped to
  Jaunt's prompt**; we do not run `colgrep --install-codex` (which edits global
  Codex config) by default. We may document `--install-codex` as an optional
  user convenience.

`search.py` shells out with `subprocess`, parses `--json` (absolute paths),
times out defensively, and treats any failure as "no hits" (never fatal).

## Configuration

New `[context]` section in `jaunt.toml`, parsed in `config.py` following the
`SkillsConfig` pattern:

```toml
[context]
repo_map = true            # maintain treedocs.yaml + inject the repo map
repo_map_file = "treedocs.yaml"
enrich = false             # opt-in LLM enrichment of descriptions
max_chars = 6000           # cap for the injected repo-map block

[context.search]           # colgrep
enabled = false            # opt-in; requires the colgrep binary
inject_agent_instructions = true
internal_retrieval = true
max_hits = 8
```

Dataclasses (frozen): `ContextConfig` (with nested `ContextSearchConfig`).
Defaults chosen so a fresh install is **offline + free**: repo-map on (AST-only),
enrichment off, colgrep off.

## CLI

Add a `tree` subcommand (argparse, via `_build_parser()` + dispatch):

- `jaunt tree` — sync + (re)generate `treedocs.yaml`. Flags: `--force`
  (regenerate all descriptions), `--no-enrich` (force AST-only even if config
  enables enrichment), `--json` (machine-readable summary), plus common flags
  (`--root`, `--config`).
- `jaunt tree --check` — deterministic drift gate (ghost/missing paths or
  signature mismatch). No model. **Exit code 4** on drift, matching the existing
  `check` convention; 0 when clean.

Integration into existing commands:
- `build` (and `test` via build): run `tree.sync()` before prompt assembly when
  `repo_map` is on; ensure colgrep index when search is enabled. Honors
  `--no-auto-skills`-style opt-outs via a new `--no-repo-map` flag.
- `watch`: on change, sync affected tree entries and let colgrep reindex.
- `status`: report tree drift (counts of new/ghost/stale entries) in text and
  `--json`.

## Data flow (build)

```
jaunt build
  └─ load config → repo_map on?
       ├─ tree.sync(roots): walk fs, diff vs treedocs.yaml
       │     ├─ add new .py/dir entries, drop ghosts
       │     ├─ for changed digests: describe.ast_describe()  (always)
       │     └─ if enrich: describe.enrich(changed, backend)  (batched, fallback)
       ├─ tree.write(treedocs.yaml) + recompute signature
       └─ search enabled? → search.ensure_index(root)
  └─ per module → _component_payload():
       ├─ block.render_repo_map(treedoc, max_chars)        → repo_map_block
       ├─ search.query(spec_text)                           → relevant_code_block
       ├─ block.render_search_hint()                        → search_hint_block
       └─ block.annotate_package_tree(...)                  → package_context_block
  └─ codex_backend._build_prompt() appends the blocks → codex exec
```

## Error handling & graceful degradation

- Missing/corrupt `treedocs.yaml` → rebuild from scratch (treat as empty),
  log once.
- **YAML dependency:** Jaunt has no YAML library today (base deps: `rich`,
  `watchfiles`, `pytest`, `pytest-asyncio`, `anyio`). The treedocs format
  requires real YAML round-tripping of human-edited files, so `pyyaml` is added
  as a base dependency (ubiquitous, pure-Python fallback available). This is the
  one new runtime dependency the subsystem introduces.
- Enrichment failure (no backend, bad JSON, timeout) → AST baseline; build
  proceeds.
- colgrep absent/disabled/error/timeout → empty `relevant_code_block` +
  `search_hint_block`; optional ripgrep fallback; build proceeds.
- `jaunt tree --check` is the only place drift is fatal (exit 4), and only when
  explicitly invoked.

## Testing strategy

- **Unit (offline, deterministic):**
  - `describe.ast_describe()` over fixture modules (docstring / decorators /
    empty) → exact strings.
  - `tree.sync()` add/ghost/stale diffing against a temp tree; `signature()`
    stability and drift detection.
  - `block.render_repo_map()` capping behavior at `max_chars`.
  - `config.py` parsing of `[context]` / `[context.search]` incl. defaults.
  - CLI `jaunt tree` / `--check` exit codes via the existing CLI test harness.
- **Mocked backend:** `describe.enrich()` with a fake backend returning JSON;
  and the fallback path when the backend raises.
- **colgrep:** `search.py` with `subprocess`/`which` mocked (present → parses
  JSON; absent → fallback/empty). No test requires the real binary or network.
- **Prompt assembly:** `_build_prompt()` includes/excludes each block per
  config flag.

The full suite stays offline and key-free, consistent with Jaunt's existing
mocked-backend tests.

## Incremental landing plan (all behind flags)

1. **Tree core + injection:** `tree.py`, `describe.py` (AST only), `block.py`,
   `ContextConfig`, `jaunt tree`/`--check`, repo-map injection + package-tree
   annotation. Default on, offline.
2. **Enrichment:** `describe.enrich()` + `[context] enrich`, batched + cached +
   fallback.
3. **colgrep:** `search.py`, `[context.search]`, `relevant_code_block` +
   `search_hint_block`, index freshness, fallbacks.

## Open questions / risks

- **Repo-map size vs. value:** a large repo could blow `max_chars`; capping
  drops the least-relevant lines. We may later prioritize the building module's
  neighborhood. Acceptable for v1.
- **Enrichment cost cadence:** batching + digest-caching keeps it cheap, but on
  a first full enrichment of a big repo it is one larger call. Documented;
  `--no-enrich` and default-off mitigate.
- **treedocs schema evolution:** pinned to v0.2.0; a schema bump is a follow-up.
- **colgrep query relevance:** depends on the model colgrep uses; we expose
  `max_hits` and treat hits as additive context, never authoritative.

## References

- next-plaid / colgrep: https://github.com/lightonai/next-plaid
- colgrep CLI README: https://github.com/lightonai/next-plaid/blob/main/colgrep/README.md
- treedocs: https://github.com/DandyLyons/treedocs
- treedocs schema v0.2.0: https://dandylyons.github.io/treedocs/schemas/0.2.0/treedocs.schema.json
