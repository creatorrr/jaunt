# jaunt.ing Docs Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is executed by a dynamic workflow of opus WRITER agents (prose written directly; codex is not the author) in a dedicated worktree; each task is one workflow unit.

**Goal:** Rebuild jaunt.ing as a Diátaxis-lite docs site that wins over a skeptical Python dev: 4-quadrant IA, 3 executed tutorials, completed reference, a real landing page, zero scaffold leftovers.

**Architecture:** One scaffolding task establishes the new tree + redirects + chrome; four parallel content tasks own one quadrant directory each (disjoint files); landing page follows; a verification wave (build, links, coverage checklist, fresh-eyes reader) gates the PR.

**Tech Stack:** Fumadocs 16.5 / Next 16 static export, MDX, Tailwind v4 (stock theme), GitHub Pages via `.github/workflows/docs-pages.yml`.

**Spec:** `docs/superpowers/specs/2026-07-04-docs-overhaul-design.md` — READ FIRST, especially §1 (exact IA) and §3 (voice contract).

## Global Constraints

- Worktree: all work on branch `feat/docs-overhaul` in a dedicated worktree; site root is `docs-site/`.
- Stock Fumadocs styling only — no new npm dependencies, no custom theme.
- Voice contract (spec §3) binds every page: tutorials never explain, guides start from a task, concepts never list flags, reference is versionless in tone (version churn only in `reference/upgrading.mdx`).
- JWT example appears ONLY on the landing page and `tutorials/quickstart.mdx`. Other tutorials use fresh domains (suggested: rate limiter for adopt-existing; order-book class for whole-class-daemon).
- Every command shown in a tutorial MUST have been executed against installed jaunt 1.3.0 (`uv tool install jaunt` or `uvx --from jaunt jaunt ...`) in a scratch project; paste real output (trim noise, never invent). Codex CLI is authenticated on this machine; builds cost real API dollars — keep scratch specs small (one or two functions/classes).
- Prose follows the natural-writing skill (invoke it before writing): no LLM-tell phrasing, no marketing superlatives inside docs pages, no "In this guide, we will…" throat-clearing.
- After each task: `cd docs-site && npm run build` must pass. Commit per task with `docs(site): …` messages ending `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. git add only files you own.
- File-ownership discipline: content tasks own exactly their quadrant directory; only Task 1 touches meta.json files, `app/`, `lib/`, `components/`.

## Wave map

| Wave | Tasks | Parallel? |
|------|-------|-----------|
| 1 | 1 (scaffolding + redirects + chrome) | solo |
| 2 | 2 (tutorials), 3 (guides), 4 (concepts), 5 (reference) | yes — disjoint dirs |
| 3 | 6 (landing page + docs home) | after wave 2 (links must resolve) |
| 4 | 7 (verification + fixes) | last |

---

### Task 1: IA scaffolding, redirect stubs, chrome cleanup

**Files:**
- Create: `docs-site/components/moved.tsx` (client redirect component)
- Create: redirect stub MDX at every moved slug (list below)
- Create: `docs-site/content/docs/tutorials/meta.json`, `concepts/meta.json`; update `guides/meta.json`, `reference/meta.json`, root `content/docs/meta.json`
- Modify: `docs-site/app/og/docs/[...slug]/route.tsx` (site name), `docs-site/lib/layout.shared.tsx` (nav links), `docs-site/README.md` (rewrite)
- Move (git mv): `writing-specs/magic.mdx → guides/writing-magic-specs.mdx`, `writing-specs/test-specs.mdx → guides/writing-test-specs.mdx`, `writing-specs/dependencies.mdx → guides/dependencies.mdx`, `writing-specs/tips.mdx → guides/spec-tips.mdx`, `how-it-works.mdx → concepts/how-jaunt-works.mdx`, `guides/jwt-walkthrough.mdx → tutorials/quickstart-source-2.mdx` (temporary staging for Task 2 to merge; delete in Task 2), `quickstart.mdx → tutorials/quickstart.mdx`, `reference/codex-engine.mdx → concepts/codex-engine.mdx`, `reference/change-detection.mdx → concepts/change-detection.mdx`, `reference/repo-context.mdx → concepts/repo-context.mdx`, `reference/output.mdx → reference/output-layout.mdx`
- Delete: `development/` (both pages), `guides/adding-to-your-project.mdx` content merges into Task 2's adopt-existing (git mv it to `tutorials/adopt-existing.mdx` as raw material)

**Interfaces:**
- Produces: the final directory tree of spec §1 with MOVED-BUT-UNEDITED content (content rewriting is waves 2–3); `<Moved to="/docs/..." />` component; every old slug resolving.

- [ ] **Step 1:** Create the worktree: from repo root `git worktree add ../jaunt-docs-overhaul -b feat/docs-overhaul && cd ../jaunt-docs-overhaul/docs-site && npm ci`.
- [ ] **Step 2:** Write `components/moved.tsx`:

```tsx
'use client';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

export function Moved({ to }: { to: string }) {
  const router = useRouter();
  useEffect(() => { router.replace(to); }, [router, to]);
  return (
    <p>
      This page moved to <a href={to}>{to}</a>.
    </p>
  );
}
```

- [ ] **Step 3:** Perform the git mv moves listed above; create the new meta.json files with the spec §1 nav order (root order: `index`, `tutorials`, `guides`, `concepts`, `reference`; titles "Tutorials", "Guides", "Concepts", "Reference").
- [ ] **Step 4:** At each OLD slug, create a stub MDX. Template (adjust title/target):

```mdx
---
title: Moved
---
import { Moved } from '@/components/moved';

<Moved to="/docs/guides/writing-magic-specs" />
```

Old slugs needing stubs: `writing-specs/magic`, `writing-specs/test-specs`, `writing-specs/dependencies`, `writing-specs/tips`, `how-it-works`, `quickstart`, `guides/jwt-walkthrough`, `guides/adding-to-your-project`, `reference/codex-engine`, `reference/change-detection`, `reference/repo-context`, `reference/output`, `development/contributing` (→ GitHub repo URL), `development/architecture-notes` (→ `/docs/concepts/how-jaunt-works`). Add a `writing-specs/meta.json` + `development/meta.json` with `"pages": [...]` and `"root": false` if needed to keep stubs OUT of the sidebar (verify Fumadocs hides them; if not, set frontmatter or meta to exclude — the stubs must not appear in nav).
- [ ] **Step 5:** Chrome: OG route `site="Jaunt"`; `layout.shared.tsx` add links `[{ text: 'GitHub', url: 'https://github.com/creatorrr/jaunt', external: true }, { text: 'PyPI', url: 'https://pypi.org/project/jaunt/', external: true }]`; rewrite `docs-site/README.md` (~15 lines: what the site is, `npm run dev`, `npm run build`, deploy-on-merge note).
- [ ] **Step 6:** Fix every internal link broken by the moves in the moved-but-unedited pages (grep `](/docs/` across content/, update paths). `npm run build` → green. Commit: `docs(site): diátaxis-lite scaffolding, redirect stubs, chrome cleanup`.

### Task 2: Tutorials quadrant (3 pages, executed)

**Files:**
- Rewrite: `docs-site/content/docs/tutorials/quickstart.mdx` (merge best of old quickstart + jwt-walkthrough; DELETE `tutorials/quickstart-source-2.mdx` after merging)
- Rewrite: `docs-site/content/docs/tutorials/adopt-existing.mdx` (old adding-to-your-project is raw material)
- Create: `docs-site/content/docs/tutorials/whole-class-daemon.mdx`

**Interfaces:**
- Consumes: Task 1's tree. Produces: three tutorials whose slugs Task 6 links to.

- [ ] **Step 1:** Invoke the natural-writing skill. Read spec §3 voice contract + the three per-page briefs below.
- [ ] **Step 2:** Build scratch projects under `/tmp/jaunt-docs-scratch/<name>` with jaunt 1.3.0 (`uv init && uv add jaunt`), run every command you document, capture real output.
  - **quickstart.mdx** (~10 min read): install + `codex login` → `jaunt init` → one `@jaunt.magic` JWT `create_token`/`verify_token` spec → `jaunt build` (show real progress + cost line) → import and call it → one `@jaunt.test` → `jaunt test`. End: cards to the other two tutorials.
  - **adopt-existing.mdx**: start from a small existing project (write a realistic ~80-line rate-limiter module + tests in the scratch repo first); add jaunt.toml (show strict-config catching a deliberate typo'd key — real error output with the did-you-mean), convert ONE leaf function to `@jaunt.magic`, `jaunt build`, existing tests stay green, `.pyi` appears (show it), coverage `exclude_lines` recipe, wire `jaunt check` into CI (snippet), commit conventions for `__generated__/`.
  - **whole-class-daemon.mdx**: docstring-only `OrderBook` class spec → build → show designed API from the `.pyi`; add one `@jaunt.preserve` helper + one `@jaunt.sig` sealed method + leave one guidepost stub, rebuild, show tier behavior; then `jaunt daemon start` → edit spec → commit → `jaunt jobs` shows the proposal → `jaunt jobs land <id>` → `jaunt daemon stop`. Real outputs throughout.
- [ ] **Step 3:** `npm run build` green; every internal link resolves. Commit: `docs(site): tutorials quadrant — quickstart, adopt-existing, whole-class-daemon (executed against 1.3.0)`.

### Task 3: Guides quadrant

**Files:** rewrite in place: `guides/writing-magic-specs.mdx`, `guides/writing-test-specs.mdx`, `guides/dependencies.mdx`, `guides/spec-tips.mdx`, `guides/contract-mode.mdx`, `guides/daemon.mdx`, `guides/coding-agents.mdx`, `guides/pypi-skills.mdx`; create `guides/ci.mdx`; update `guides/meta.json` order: writing-magic-specs, writing-test-specs, dependencies, spec-tips, contract-mode, daemon, ci, coding-agents, pypi-skills.

**Interfaces:** consumes Task 1 tree; produces guide slugs Task 6 links.

- [ ] **Step 1:** Invoke natural-writing. Recast every page to open from the reader's task ("You want generated code to call your existing helpers" etc.), shortest path first. Keep the strong existing material (tier table in writing-magic-specs; landing modes in daemon) — this is editing, not greenfield. Strip any "changed in 1.x" phrasing (move the fact to a one-line pointer to `/docs/reference/upgrading`).
- [ ] **Step 2:** Write `guides/ci.mdx` (NEW): "Gate generated code in CI." `jaunt check` as the single deterministic gate (no API key), exit-code table, `--contracts-only`/`--magic-only`, `--json` for annotations, `jaunt tree --check`, the `jaunt guard` PreToolUse hook for agent repos, and a complete GitHub Actions job snippet (checkout → uv → `uvx --from jaunt jaunt check`).
- [ ] **Step 3:** `guides/coding-agents.mdx` additionally documents the llms.txt endpoints (`/docs/<page>/index.txt` via getLLMText) and `jaunt instructions --json`.
- [ ] **Step 4:** `npm run build` green. Commit: `docs(site): guides quadrant — task-first rewrites + new CI guide`.

### Task 4: Concepts quadrant

**Files:** rewrite in place: `concepts/how-jaunt-works.mdx`, `concepts/change-detection.mdx`, `concepts/repo-context.mdx`, `concepts/codex-engine.mdx`; create `concepts/design-philosophy.mdx`; create `concepts/meta.json` order: how-jaunt-works, design-philosophy, change-detection, repo-context, codex-engine.

- [ ] **Step 1:** Invoke natural-writing. Concepts explain and compare; move any flag/key enumeration to a pointer at Reference. `how-jaunt-works.mdx` ends with a short "Internals" paragraph pointing at the repo's `docs/superpowers/` + principles doc (replaces deleted architecture-notes).
- [ ] **Step 2:** Write `concepts/design-philosophy.mdx` (NEW): why specs-as-contracts beats raw prompting (pull the "Why not prompt an LLM directly" section from the old index.mdx); the docstring-is-the-contract principle; determinism boundaries (what's deterministic — digests, check, validation — vs what's not — generation); an honest "when NOT to use jaunt" section (pull from spec-tips/limitations: tiny scripts, hot-path perf code, teams unwilling to review generated diffs); cost expectations paragraph with a pointer to `context_stats`.
- [ ] **Step 3:** `npm run build` green. Commit: `docs(site): concepts quadrant + design-philosophy`.

### Task 5: Reference quadrant

**Files:** rewrite: `reference/cli.mdx`, `reference/config.mdx`, `reference/output-layout.mdx`; keep+touch: `reference/limitations.mdx`; create: `reference/json-output.mdx`, `reference/upgrading.mdx`; update `reference/meta.json` order: cli, config, json-output, output-layout, limitations, upgrading.

- [ ] **Step 1:** `cli.mdx`: regroup commands by workflow (Authoring: init/specs/instructions · Building: build/watch/status/clean · Testing: test · Contract: adopt/reconcile/check/eject · Daemon: daemon/jobs/log/guard · Context: tree · plus cache/skill). Per command: one-line purpose, synopsis, flag table, exit codes, link to its json-output section. Source of truth: `uvx --from jaunt jaunt --help` + per-command `--help` (run them; do not trust the old page).
- [ ] **Step 2:** `config.mdx`: single annotated TOML block aligned with `src/jaunt/init_template.py::FULL_SCHEMA_TEMPLATE` (read it in the repo — it is allowlist-complete by test); then a **Strict validation** section: unknown section/key → exit 2 with did-you-mean (show a real error), `jaunt instructions` prints this schema pre-init. Delete the inline "Changelog (1.2.0)" (facts move to upgrading.mdx).
- [ ] **Step 3:** `json-output.mdx` (NEW): conventions (`command`, `ok`, errors→stderr, progress suppressed); then per command the real payload: run `build`, `status`, `check`, `specs`, `jobs`, `test --no-run`, `clean --dry-run`, `instructions` with `--json` in a scratch project and paste trimmed real output; document 1.3 keys explicitly: `needs_deps`, `context_stats`, `refrozen`, check's `magic` block. Exit-code table repeated here verbatim from cli.mdx.
- [ ] **Step 4:** `output-layout.mdx`: generated-path rules incl. 1.3 top-level `__generated__/<module>.py` + PEP 420 namespace note; provenance header anatomy (annotated real header); `.pyi` stub anatomy + freshness + never-overwrite rule; what `clean` removes.
- [ ] **Step 5:** `upgrading.mdx` (NEW): per-version migration notes — 1.3.0 (layout migration `jaunt clean && jaunt build`, strict config may surface latent typos, `@sig` preferred vocabulary, `emit_stubs` default-on, check now gates magic drift, repo-map decoupling) and 1.2.0 (propose-only daemon default, `auto_commit=true` to restore). Link to GitHub releases for the full changelog. Sweep OTHER reference/guide pages for leftover version-churn phrasing and replace with links here (coordinate: this task owns the sweep within reference/; tasks 3–4 already stripped theirs).
- [ ] **Step 6:** `npm run build` green. Commit: `docs(site): reference quadrant — workflow-grouped CLI, strict config, JSON contract, upgrading`.

### Task 6: Landing page + docs home

**Files:** rewrite `docs-site/app/(home)/page.tsx`; rewrite `docs-site/content/docs/index.mdx`.

**Interfaces:** consumes final slugs from tasks 2–5.

- [ ] **Step 1:** `page.tsx` per spec §2: hero (headline + one-liner + CTAs `Quickstart` → `/docs/tutorials/quickstart`, `GitHub`; PyPI badge img `https://img.shields.io/pypi/v/jaunt`), wow-gap side-by-side (spec left, trimmed generated code right — adapt the panel from the old index.mdx; keep it server-rendered, plain `<pre>` blocks with Tailwind grid), four feature cards, honest strip linking `/docs/concepts/design-philosophy` + `/docs/reference/limitations`. No new deps.
- [ ] **Step 2:** `index.mdx` (docs home): 3 short paragraphs on what jaunt is; `<Cards>` for the four quadrants ("Start here" → quickstart; "Get things done" → guides; "Understand it" → concepts; "Look it up" → reference); a two-route strip: evaluating human → quickstart; coding agent → guides/coding-agents (+ llms.txt mention). Compact wow-gap allowed (a few lines), not the full panel.
- [ ] **Step 3:** `npm run build` green. Commit: `docs(site): landing page + docs home`.

### Task 7: Verification wave + fixes

- [ ] **Step 1:** Full build: `cd docs-site && npm run build` — zero errors/warnings that matter.
- [ ] **Step 2:** Link check over the export: `npx --yes linkinator out --recurse --skip 'https?://(?!jaunt\.ing)'` (internal links only) — zero broken. Fix any.
- [ ] **Step 3:** Coverage checklist: for every feature bullet in repo `CLAUDE.md` (magic, tiers/@sig, contract mode, daemon+propose-only, check gate, .pyi stubs, strict config, repo-map/tree, semantic gate, skills, JSON, exit codes, watch, guard, instructions), record which page covers it. Any gap → fix now. Paste the completed checklist into the task output.
- [ ] **Step 4:** Redirect stubs: `grep -L "Moved" $(list of old-slug files)` → every old slug must render the Moved component; spot-check 3 in the built out/.
- [ ] **Step 5:** OG check: built OG route/metadata contains "Jaunt", not "My App" (`grep -r "My App" docs-site/ --include='*.tsx' --include='*.ts'` → empty).
- [ ] **Step 6:** Fresh-eyes reader: dispatch a context-free reader agent with ONLY the built page text (out/**/index.txt) and the persona "skeptical Python dev, 15 minutes, deciding whether to try jaunt"; collect friction points; fix blocking ones (confusing sequence, unexplained jargon before its concept link, dead ends).
- [ ] **Step 7:** Final commit `docs(site): verification fixes`, push branch, open PR titled `docs: jaunt.ing overhaul — Diátaxis-lite IA, executed tutorials, landing page`. Docs-only: no pyproject bump. Merge deploys via docs-pages.yml.

## Self-review notes

- Spec coverage: §1 IA → Tasks 1–5; §2 landing → Task 6; §3 voice/dedup/executed-samples → Tasks 2–5 steps; §4 chrome → Task 1 (OG, README, nav) + Task 3 (llms.txt surfacing); §5 redirects → Task 1 + Task 7 step 4; §6 verification → Task 7; §7 execution → wave map.
- Consistency: slugs referenced in Tasks 6–7 match Tasks 1–5 tree; `Moved` component name consistent; meta.json orders stated once per owner task.
