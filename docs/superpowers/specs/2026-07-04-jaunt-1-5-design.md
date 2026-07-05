# jaunt 1.5 Design — skills trust-and-verify, advisories, migrate, reconciliation

Date: 2026-07-04
Status: approved (brainstormed + codex@medium design review, corrections folded in)
Driver: adoption feedback findings 19, 20, 25, 26 (mem-mcp-b campaign, waves 2–4)
plus deletion-lifecycle research.

## Goals

- Close the remaining adoption-feedback backlog: skills cost (19), skill
  frontmatter dupe (20), free legacy-stub migration (25), newly-governed
  visibility (26), orphaned-artifact lifecycle.
- Add an advisories channel: codex reports logical issues it noticed during
  generation; jaunt surfaces them in build output, `--json`, the journal, and
  daemon job records.
- **Zero mass invalidation on upgrade.** No adopter module may restale from
  upgrading to 1.5 alone. Every stream below that touches prompts, skills, or
  fingerprints carries an explicit verification/carve-out requirement.

## Non-goals

- No per-module skill pruning/filtering machinery (held in reserve; only
  revisit if the stream-1 probe shows codex eagerly reading skill bodies).
- No advisory severity/category taxonomy (freeform lines first; grow an enum
  later from observed data).
- No `[llm]` config-block deprecation (deferred again).
- No ty config scaffolding in `jaunt init` (docs callout only).

---

## Stream 1 — Skills: trust-and-verify (findings 19 + 20)

**Verified premise** (against source): skills are seeded as proper `SKILL.md`
dirs into `.agents/skills/` (`skill_seed.py:29`); the build prompt only says
"Consult them when they apply" (`codex_backend.py:423`); and
`context_stats.skills_workspace` is measured as total on-disk bytes of seeded
SKILL.md files (`builder.py:88-101`) — worst-case exposure, not consumption.
The adopter's "skills are ~95% of context" is therefore possibly a
measurement artifact of our own reporting.

### 1a. Fix the double-YAML-frontmatter bug (finding 20)

Root cause (codex-review confirmed): `skillgen.py:53` asks codex to output a
*full* SKILL.md; `skill_agent.py:24` validation only checks headings; then
`skills_auto.py:122` unconditionally prepends jaunt's own frontmatter →
duplicate `---` blocks on skills where the model included its own.

Fix: before prepending frontmatter, detect and strip an existing leading YAML
frontmatter block from the model output; add validation at emission time that
the final written SKILL.md contains exactly one frontmatter block (reject and
retry/repair otherwise). Repair existing affected skills on the next
regeneration; no forced regeneration.

This fix is potentially load-bearing for cost, not cosmetic: malformed
frontmatter may defeat codex's native lazy skill scanner and force full-body
reads.

### 1b. Light empirical probe (one-off, during implementation)

One instrumented build on an example project inspecting codex's `--json`
event stream (file reads / turn contents as available) to answer: does codex
open skill bodies on demand, or eagerly? Result is recorded in the PR
description and drives whether pruning (non-goal) gets a 1.6 design. Not a
shipped feature; no test-suite footprint beyond what 1a/1c need.

### 1c. Honest `context_stats` labeling

`skills_workspace` today reads as "this was sent to the model." Rename the
reported block to make it unambiguous (e.g. `skills_workspace_seeded`, with
docs: "total bytes seeded on disk and *available* to the agent — not
necessarily read"). Keep the old key as an alias in `--json` for one release
if trivially cheap; otherwise document the rename in the changelog and
upgrading page (it is informational output, not config — acceptable to
rename).

---

## Stream 2 — Codex advisories channel

Codex reports logical issues it noticed while generating (spec ambiguity,
contradictions between a spec and its deps' docs, suspected bugs in deps it
read). Jaunt surfaces them. Informational only: no digest participation, no
staleness effect, no exit-code effect.

### Transport (codex-review corrected)

The backend already runs `codex exec --json -` and parses the final JSONL
`agent_message` (`codex_backend.py:127`, `codex_backend.py:192`), then
discards it. Design:

- Build and test prompts gain a short instruction: end the final message with
  an `ADVISORIES:` block, one item per line, plain prose; write `ADVISORIES:
  none` if nothing to report. (Exact wording tuned at implementation.)
- Parse the block from the already-captured final agent message in
  `codex_backend.py`. Lenient parser: missing block → no advisories;
  malformed content under the heading → kept as raw advisory text, never
  dropped.
- `GenerationResult` (`generate/base.py:60`) gains `advisories: list[str]`
  (default empty). Threaded through `generate_with_retry` → builder/tester
  per-module results.

### Surfacing

- End of `jaunt build` / `jaunt test`: an "Advisories" section listing
  `module: text` lines for this run's fresh generations (suppressed when
  empty; suppressed entries never printed as "none").
- `--json`: `"advisories": {"<module>": ["...", ...]}` on build/test output.
- Journal: one `JAUNT_LOG` line per advisory (event `advisory`, module,
  text), so `jaunt log` replays them.
- Daemon: advisories persist into job records; `jaunt jobs show <id>`
  displays them; daemon log lines include them on job completion.

### Fingerprint carve-out (hard requirement)

The advisories prompt instruction MUST NOT enter the generation fingerprint.
It is behavior-neutral by construction — it changes only the final chat
message, never the generated module. Implementation must verify where the
hardcoded prompt blocks in `codex_backend.py` participate in freshness
fingerprinting and add the advisories instruction outside that boundary (or
explicitly exclude it). Acceptance: build a project on 1.4.2, upgrade to 1.5,
`jaunt status` shows zero stale modules.

Retries: advisories from failed attempts are discarded; only the final
successful attempt's advisories are reported.

---

## Stream 3 — `jaunt migrate`

First jaunt command that edits user-authored spec source files. Posture:
**plan-by-default**. `jaunt migrate` prints what it would change and exits 0;
`jaunt migrate --apply` executes. `--json` supported in both modes.

Internally a small versioned migration registry ("pending mechanical
migrations"); each migration can report (per module/symbol) what it would do.
1.5 ships two migrations:

### 3a. Legacy stub-body rewrite (finding 25)

Rewrites `raise RuntimeError("spec stub")` spec bodies to `...` and re-stamps
headers so the conversion is free.

**Mechanism (codex-review corrected):** the two body forms are NOT
digest-equal — Layer-A normalization elides only recognized stub bodies
(`digest.py:290`, `class_analysis.py:67`), and `raise RuntimeError` is not
one (regression-tested at `test_magic_module_digest.py:134`). Migrate makes
the rewrite free by **deliberate re-stamp**: jaunt itself performed the
rewrite and vouches the behavioral contract is unchanged, so it recomputes
the new contract digest and drives the existing refreeze machinery
(`builder.py:384`) to rewrite the generated header over the untouched
generated body. No model call.

**Governance-change guard (codex-review hazard):** in module mode a
`raise RuntimeError("spec stub")` body is not a recognized stub and is
therefore *ungoverned* handwritten context (`module_magic.py:184`). Rewriting
such a body to `...` would CREATE a new module-origin spec. The plan output
must classify every candidate:

- `re-stamp (free)` — the symbol is already a spec (decorator-governed, or
  otherwise already in the governed set); rewrite + re-stamp, $0.
- `would newly govern` — the symbol is currently ungoverned; rewriting
  changes discovery state and commissions a first build. NOT applied by
  default; requires `--apply --allow-newly-governed` (name final at
  implementation). Cross-references the stream-4 newly-governed labeling.

Scope: only exact `raise RuntimeError("spec stub")` bodies (optionally with a
docstring above), matching what the legacy scaffold emitted. Arbitrary
RuntimeError raises are never touched.

### 3b. Stub re-emission for format-version bumps

When committed `.pyi` stubs are stale solely because `_STUB_FORMAT_VERSION`
bumped (`stub_emitter.py:15`), `jaunt migrate --apply` re-emits them (and
re-stamps stub-freshness bookkeeping) without a build. Retires the 1.4.2
upgrade wart ("`jaunt check` exits 4 until you run `jaunt build` once").
Note: the refreeze path does not emit stubs, so this migration re-emits
explicitly.

Safety: `--apply` refuses on a dirty git working tree unless `--force` (plan
mode always allowed). Every applied change is listed file-by-file.

---

## Stream 4 — Reconciliation: newly-governed labeling + orphan lifecycle

One reconciliation pass comparing the current governed spec set (discovery)
against provenance-headed artifacts on disk.

### 4a. Newly-governed visibility (finding 26)

`jaunt check` ALREADY exits 4 on any unbuilt magic module (`cli.py:1960`), so
CI is already gated; this stream is labeling and pre-spend visibility only:

- `jaunt build` plan output flags module-origin specs with no prior artifact:
  `newly governed by module scan: <module>.<symbol> — first build`, printed
  before generation starts (before money is spent).
- `jaunt specs` (text + `--json`) marks these entries (`"newly_governed":
  true`). `SpecEntry.origin` already distinguishes module vs decorator origin
  (`registry.py:43`, `module_magic.py:417`).
- `jaunt check`'s existing unbuilt-module failure message gains the same
  phrasing for module-origin entries.
- Daemon job records include the newly-governed list for the job's build.

Decorator-origin specs are never flagged (always intentional).

### 4b. Orphan lifecycle

Orphan: an artifact on disk whose spec no longer exists. Attribution
(codex-review verified): generated `.py` via `source_module` + `spec_refs`
headers (`header.py:75`); `.pyi` via `source_module` (`header.py:135`);
contract batteries via `derived-from` (`header.py:17`); `.contract.json`
sidecars by adjacency to their generated module (removed with it).

- `jaunt check`: orphans BLOCK with exit 4; the message names the fix
  (`jaunt clean --orphans`, or restore the spec). Accepted consequence: a
  mid-refactor rename shows one orphan + one newly-governed and holds CI red
  until rebuilt — intentional.
- `jaunt status`: lists orphans in a dedicated section (`--json`:
  `"orphans"`).
- `jaunt clean --orphans`: removes only orphaned artifacts (generated module,
  its sidecar, its `.pyi`, its generated tests / contract battery), honoring
  `--dry-run`. Plain `jaunt clean` behavior unchanged.
- Journal: `clean --orphans` writes one `orphan removed: <artifact>` line per
  removal at removal time. NO persisted prior-governed-set state (the
  originally proposed "spec removed" line on set-shrink is dropped —
  codex-review: discovery has no remembered prior set, and we will not add
  state machinery for a log line).

Hand-authored files are never candidates: only artifacts bearing jaunt
provenance headers (or sidecars adjacent to them) can be classified orphaned.

---

## Stream 5 — Docs

### 5a. Stub-form guidance flips to `...`

Scaffold (`init_template.py`), README, quickstart, landing page, and guides
lead with `...` bodies again. Rationale: runtime is fully covered —
magic_module installs not-built raisers, so `raise NotImplementedError` is
type-checker appeasement only. A callout documents the ty empty-body
complaint with two equal alternatives: relax the ty rule on spec roots, or
write `raise NotImplementedError`; the forms are digest-identical either way.
jaunt does NOT scaffold ty config.

### 5b. Independent docs-site fresh-eyes pass

A dedicated sub-agent (no implementation context) sweeps the entire docs-site
for drift accumulated across the 1.3.0/1.3.1/1.4.x same-day releases:
contradictions, stale stub-form guidance, dead anchors, upgrading-page
consistency, examples that no longer match current CLI output. Findings fixed
in the same PR. The natural-writing skill applies to all prose edits.

---

## Stream 6 — Re-stamp labeling (small)

`jaunt status`/`check` label a module `stale (structural)` even when the next
build resolves it via the free refreeze/re-stamp path (adopter-verified
confusion, @sig alias migration case). Add a status-side predictor: when the
staleness cause is one the refreeze path is known to resolve without a model
call, label it `stale (re-stamp: free)` (text final at implementation) in
`status` and `check` output, and where applicable hint `jaunt migrate` for
the stream-3a case.

---

## Upgrade & compatibility requirements (release gate)

1. Build a project on 1.4.2, upgrade to 1.5: `jaunt status` reports zero
   stale modules; `jaunt check` exits 0 (given no pre-existing drift).
   Specifically verify: (a) the advisories prompt instruction is outside the
   generation fingerprint; (b) the skills frontmatter fix does not restale
   modules via `skills_fingerprint` participation (`cli.py:2561`,
   `generate/base.py:30`) — if it does, carve out or defer regeneration of
   affected skills to natural regeneration.
2. `_STUB_FORMAT_VERSION` is NOT bumped by 1.5 (no emitter format changes).
3. New CLI surface: `jaunt migrate [--apply] [--force] [--json]`,
   `jaunt clean --orphans [--dry-run]`, new JSON keys (`advisories`,
   `orphans`, `newly_governed`). No existing flags change meaning.
4. Exit codes unchanged: orphans blocking `check` reuse exit 4.

## Testing approach

Mocked-backend test suite as always (no API keys). Each stream lands with
unit tests beside the module it touches; advisories parsing gets
fixture-driven tests over final-message variants (block present / absent /
malformed / "none"); migrate gets plan-vs-apply tests over fixture projects
incl. the governance-change guard; reconciliation gets orphan/newly-governed
matrix tests over synthetic artifact trees; frontmatter fix gets
dupe-detection tests over model-output variants.
