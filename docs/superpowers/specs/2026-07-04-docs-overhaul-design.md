# jaunt.ing Docs Overhaul — Diátaxis-lite (Design)

Date: 2026-07-04
Status: Approved
Baseline: docs-site audit of 2026-07-04 (22 MDX pages, ~13.6k words, Fumadocs
16.5 / Next 16 static export → GitHub Pages). Coverage of 1.3.0 features is
largely present; the failure is packaging: skeleton landing page, scaffold
leftovers ("My App" OG images, boilerplate README, two stub dev pages),
reference-as---help-dump with changelog fragments inline, only two tutorials
(both recycling the same JWT/email examples), and thin JSON-output/strict-config
reference.

## Decisions (user-confirmed)

- Primary reader: the **skeptical human dev evaluating** jaunt. Docs must build
  trust fast; agents and existing users get explicit entry paths but don't set
  the tone.
- Scope: **content + IA + landing page**, stock Fumadocs styling (no custom
  visual identity).
- Structure: **Diátaxis-lite** — the four quadrants as nav skeleton and voice
  contract, no per-paragraph dogma.
- Execution: dynamic workflow; prose written directly by writer agents (not via
  codex — this is writing, not coding).

## 1. Information architecture

Top nav (meta.json order): docs home → Tutorials → Guides → Concepts →
Reference.

```
content/docs/
  index.mdx                       # docs home: what jaunt is (3 short paras), quadrant
                                  # cards, two entry routes: "evaluating?" → quickstart;
                                  # "coding agent?" → guides/coding-agents + llms.txt
  tutorials/
    quickstart.mdx                # rewritten from quickstart.mdx: zero → generated module
                                  # + generated test, ~10 min, JWT domain
    adopt-existing.mdx            # NEW: convert one leaf module of a realistic existing
                                  # project; strict config; check in CI; coverage
                                  # exclude_lines; .pyi stubs appear; upgrade-safe habits
    whole-class-daemon.mdx        # NEW: docstring-only class spec + three tiers
                                  # (preserve/@sig/guidepost), then the propose-only
                                  # daemon loop: commit → job → jobs land
  guides/
    writing-magic-specs.mdx       # from writing-specs/magic.mdx (tier table stays)
    writing-test-specs.mdx        # from writing-specs/test-specs.mdx
    dependencies.mdx              # from writing-specs/dependencies.mdx
    spec-tips.mdx                 # from writing-specs/tips.mdx ("getting good results")
    contract-mode.mdx             # kept
    daemon.mdx                    # kept
    ci.mdx                        # NEW: jaunt check as the CI gate (both modes, exit
                                  # codes, --json wiring, tree --check, guard hook)
    coding-agents.mdx             # kept + surfaces llms.txt/getLLMText output
    pypi-skills.mdx               # kept
  concepts/
    how-jaunt-works.mdx           # from how-it-works.mdx
    change-detection.mdx          # kept (moved)
    repo-context.mdx              # kept (moved)
    codex-engine.mdx              # kept (moved); config detail stays in reference
    design-philosophy.mdx         # NEW-ish: why spec-driven; why not raw prompting
                                  # (from index.mdx); when NOT to use jaunt (from
                                  # tips/limitations). The trust page.
  reference/
    cli.mdx                       # restructured: commands grouped by workflow
                                  # (authoring / building / testing / contract / daemon /
                                  # maintenance), consistent per-command block:
                                  # synopsis, flags, exit codes, JSON pointer
    config.mdx                    # full annotated schema aligned with
                                  # init_template.FULL_SCHEMA_TEMPLATE; strict-config
                                  # behavior (exit 2, did-you-mean, `jaunt instructions`
                                  # schema print) documented HERE; inline changelog removed
    json-output.mdx               # NEW: the --json contract per command: build (incl.
                                  # needs_deps, context_stats, refrozen), test, status,
                                  # specs, check (incl. magic block), jobs, watch, clean,
                                  # instructions; envelope conventions; exit-code table
    output-layout.mdx             # from output.mdx: generated paths (1.3 top-level
                                  # __generated__/<module>.py incl. PEP 420 namespace),
                                  # provenance headers, .pyi stubs
    limitations.mdx               # kept
    upgrading.mdx                 # NEW: per-version migration notes (1.2 → propose-only
                                  # default; 1.3 → layout migration, strict config, @sig,
                                  # emit_stubs); absorbs every inline "changed in 1.x"
                                  # fragment from other pages
```

Removed from the site: `development/contributing.mdx` (repo README/CONTRIBUTING
covers it) and `development/architecture-notes.mdx` (becomes one pointer
paragraph at the bottom of concepts/how-jaunt-works.mdx). Net ~21 pages.

## 2. Landing page (`app/(home)/page.tsx`)

Replace the H1-plus-three-links skeleton with, in order:

1. Hero: headline + one-sentence positioning + two CTAs (Quickstart, GitHub)
   and a PyPI version badge.
2. The wow-gap panel as centerpiece: side-by-side spec → generated code
   (moved/adapted from content/docs/index.mdx; the docs home keeps only a
   compact version).
3. Four feature cards: incremental digest builds · contract mode ·
   `jaunt check` CI gate · agent-native (instructions/JSON/llms.txt).
4. An honest strip: "What it costs, where it breaks" linking to
   concepts/design-philosophy + reference/limitations. Skeptics trust sites
   that show their warts.

Stock Fumadocs/Tailwind components; server component; no new dependencies.

## 3. Voice contract per quadrant

- **Tutorials** teach by doing: numbered, reproducible, every command's output
  shown, zero flag-dumps, links out for anything conceptual. Must be executed
  end-to-end against jaunt 1.3.0 during implementation; outputs pasted from
  real runs.
- **Guides** start from a task ("You want X"), shortest path first, variations
  after.
- **Concepts** explain and compare; no exhaustive flag/key lists.
- **Reference** is exhaustive, grouped, and versionless in tone (version churn
  lives only in upgrading.mdx).
- Example dedup: JWT lives on the landing page + quickstart only.
  adopt-existing and whole-class-daemon use fresh domains (e.g. a rate limiter
  / an order-book class — implementer's choice, but distinct).
- Style: natural-writing skill rules apply to all prose; code samples
  copy-pasteable with `from __future__ import annotations`; en-dash-free
  headings; no marketing superlatives inside docs pages.

## 4. Chrome & scaffold cleanup

- Fix OG route: `site="Jaunt"` (currently "My App") in
  `app/og/docs/[...slug]/route.tsx`.
- Rewrite `docs-site/README.md` (what the site is, how to run/build/deploy).
- `lib/layout.shared.tsx`: nav title "Jaunt" + links to GitHub and PyPI.
- Surface llms.txt: coding-agents guide documents the `.txt` endpoints
  (`getLLMText` already ships them).
- Delete the stale `docs-site/out/` confusion risk: ensure it stays gitignored
  (it is) — no action beyond not committing it.

## 5. Redirects for moved slugs

Static export + GitHub Pages = no server redirects. Every moved/removed slug
gets a thin stub MDX at the old path containing only a meta-refresh (via a
small shared component or raw <meta http-equiv="refresh">) plus a fallback
link, kept for one release cycle. Affected: writing-specs/* (4), how-it-works,
output → output-layout, development/* (2 → point at GitHub), plus any
casualty found during implementation. New pages need no stubs.

## 6. Verification (definition of done)

- `npm run build` green (static export) with zero broken internal links
  (link-check pass over `out/`).
- Every feature bullet in the repo CLAUDE.md maps to at least one page
  (coverage checklist executed, not eyeballed).
- All tutorial/guide commands executed against installed jaunt 1.3.0; outputs
  real. Code samples for specs actually build (`jaunt build` on a scratch
  project) — a sample that has never run does not ship.
- Fresh-eyes review: a reader agent with no prior context plays "skeptical
  Python dev with 15 minutes" against the built site content and reports
  friction; blocking friction gets fixed before merge.
- OG images verified to render "Jaunt"; search index builds.

## 7. Execution

Dedicated worktree + branch (`feat/docs-overhaul`); dynamic workflow of writer
agents (opus, prose written directly — codex only if code samples need
generating/running help); waves: (1) IA scaffolding + moves + redirects +
chrome, (2) tutorials / guides / concepts / reference clusters in parallel,
(3) landing page, (4) verification wave (build + links + coverage checklist +
fresh-eyes reader), fix, PR. Docs-only change — no version bump; merging
deploys via docs-pages.yml.
