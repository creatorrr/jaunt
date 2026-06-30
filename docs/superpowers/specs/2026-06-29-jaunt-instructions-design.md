# Design: `jaunt instructions` — a loadable agent primer

**Date:** 2026-06-29
**Status:** Approved (brainstorming) — pending implementation plan

## Problem

Jaunt ships three near-duplicate "how to use Jaunt" skill files
(`.claude/skills/jaunt/SKILL.md`, `.codex/skills/jaunt/SKILL.md`,
`jaunt-claude-plugin/skills/jaunt/SKILL.md`). They have drifted apart and are
**stale** — they still describe the `legacy`/`aider` runtimes that the Codex
engine replaced. Each one also requires a specific agent harness to install.

There is no harness-agnostic, version-matched way for an arbitrary coding agent
(Claude Code, Codex, Cursor, a bare shell, CI) to get oriented on how to operate
Jaunt *in the current project* before it starts working.

## Solution

Add a `jaunt instructions` command that prints a **tight, project-aware agent
primer** to stdout. It ships with the package, so it is always version-matched to
the installed `jaunt`. Any agent runs it once at the start of a session, loads the
output into context, and proceeds.

The static primer text becomes the **single canonical source** for "how to use
Jaunt." The three `SKILL.md` files collapse to thin stubs that point at the
command, eliminating the drift/staleness problem.

## Goals

- One harness-agnostic entry point an agent can run anywhere to learn Jaunt.
- Output is *project-aware*: it reflects the actual `jaunt.toml` and current build
  state, not just generic framework rules.
- One canonical source of primer text; no duplicated/drifting skill bodies.
- "Tight" — a primer (~120–180 lines static), not a docs dump.

## Non-goals

- Replacing the rendered docs site or `DOCS.md`.
- Trimming the `references/` files under the skill dirs (separate doc hygiene).
- Multi-provider/engine instructions (Codex is the sole engine).
- A `--output FILE` flag (shell redirection covers it).

## UX

```bash
jaunt instructions               # rendered markdown primer to stdout
jaunt instructions --json        # {command, ok, text, project:{...}} for tooling/MCP
jaunt instructions > AGENTS.md   # capture via shell redirection
```

Flags: `--json`, `--root`, `--config` (consistent with other commands). Exit code
is **0** in all normal cases, including outside an initialized project.

## Output structure

The output is **static primer + a live "Your project right now" section**.

### Static primer (canonical content, ~120–180 lines)

This is also the opportunity to correct the stale `legacy`/`aider` content.

- **Mental model** — specs are *intent*; Codex generates *implementations*. The
  agent authors and refines specs; it does **not** hand-write `@magic` bodies or
  edit files under `__generated__/`.
- **Two modes** — Magic (`@jaunt.magic` / `@jaunt.test`; docstring is canonical;
  output in `__generated__/`) vs Contract (`@jaunt.contract`; committed code is
  canonical; derived battery in `tests/contract/`).
- **Core loop** — write specs → `jaunt build` → `jaunt test` → review → iterate.
- **How to write a good spec** — explicit behavior; named exceptions; edge cases;
  full type annotations; the *whole* docstring is the contract.
- **Command + exit-code reference** — a curated, ranked, annotated table (see
  "Command surface" below) plus the four exit codes.
- **Hard rules / gotchas** — never edit `__generated__/`; never hand-write magic
  bodies; incremental freshness exists, use `jaunt status`.

### Live "Your project right now" section

Rendered from config plus a best-effort freshness probe:

- Resolved `[paths]`: `source_roots`, `test_roots`, `generated_dir`.
- Engine `codex`, model + reasoning effort, semantic-gate model, repo-map on/off.
- Whether `__generated__/` exists and a one-line freshness summary (N fresh /
  M stale, up to ~5 stale module names). Falls back to "run `jaunt status`" if the
  probe cannot run cleanly.

## Command surface (no raw `--help` dump)

The primer carries a **curated, ranked, annotated** command/exit-code table — it
tells the agent *when* to reach for each command, which raw `--help` cannot. It
does **not** inline `jaunt --help` (full recursive help is hundreds of lines; the
top-level synopsis is unranked and unannotated).

To get the one advantage raw help has — it cannot go stale — the primer adds:

1. A pointer line: *"For exact flags, run `jaunt <cmd> --help`."* The agent fetches
   precise flags itself, on demand.
2. A **drift-guard unit test**: assert every command named in the primer's table
   exists as a real subparser, and that no non-trivial subcommand is silently
   missing from the table. Authoritative *and* curated.

## Architecture & components

- **Canonical source text:** a new packaged file `src/jaunt/instructions/primer.md`,
  read via the same package-data mechanism `src/jaunt/prompts/` already uses. One
  place to edit; this is the single source of truth.
- **Render module:** `src/jaunt/instructions/__init__.py` exposing two small,
  independently testable functions:
  - `project_section(root, cfg) -> dict` — builds the structured live data
    (`paths`, `engine`, `model`, `reasoning_effort`, `semantic_gate`, `repo_map`,
    `specs_count`, `built`, `stale[]`, `fresh_count`). This dict *is* the `--json`
    `project` payload.
  - `render(primer: str, project: dict | None) -> str` — stitches markdown.
- **CLI command:** `cmd_instructions(args)` in `cli.py` plus an `instructions`
  subparser. Loads config best-effort, calls the render module, and either prints
  markdown or emits
  `_emit_json({"command": "instructions", "ok": True, "text": ..., "project": ...})`.
- **Freshness reuse:** the stale/fresh probe reuses the building blocks behind
  `jaunt status` (discovery + header digest comparison), wrapped in a tight
  `try/except` so any import/discovery failure degrades to a "run `jaunt status`"
  line rather than crashing the primer. We deliberately do **not** refactor the
  monolithic `cmd_status` for v1 — best-effort keeps the change contained. A future
  enhancement may extract a shared status-summary helper that both commands call.

## SKILL.md collapse (the de-drift)

The three `jaunt/SKILL.md` bodies shrink to thin stubs: keep the frontmatter
(name/description/triggers, so skill discovery still works) and a short body that
says *"Run `jaunt instructions` for the current, project-aware workflow primer."*
Include a 3-line "hard rules" fallback (don't edit `__generated__/`, don't
hand-write magic bodies, pair specs with tests) so the skill is still minimally
useful if the CLI cannot be run.

- `.claude/skills/jaunt/SKILL.md`
- `.codex/skills/jaunt/SKILL.md`
- `jaunt-claude-plugin/skills/jaunt/SKILL.md`

`references/` under those dirs is left as-is (out of scope).

## Error handling & edge cases

- **No `jaunt.toml`** (run outside an initialized project): print the static primer
  plus a `> No jaunt.toml found — run \`jaunt init\` to start.` note; `project` is
  `null` in JSON; exit **0**.
- **Config present but freshness probe fails** (e.g. import error in user specs):
  render the project section minus the freshness line, which becomes "run
  `jaunt status`". Exit **0**.
- **`--json`:** always valid JSON, including the no-project case.

## Testing

- `render` / `project_section` unit tests: the primer contains the invariants
  (never edit `__generated__/`; both modes present; the core commands present); the
  project section reflects a custom `generated_dir` / `model`.
- No-project path → primer + init note, exit 0, valid JSON with `project: null`.
- Probe-failure path → still exits 0 with the degraded freshness line.
- Drift-guard test → every command in the primer table maps to a real subparser; no
  non-trivial subcommand missing.
- CLI smoke: `jaunt instructions` exits 0; `jaunt instructions --json` parses and has
  the documented keys (`command`, `ok`, `text`, `project`).

## Rollout

1. Add `src/jaunt/instructions/` (primer text + render module).
2. Wire the `instructions` subparser + `cmd_instructions` into `cli.py`.
3. Collapse the three `SKILL.md` bodies to stubs.
4. Document the command in `CLAUDE.md` and the README CLI list.
5. Tests as above; run `uv run ruff check .`, `uv run ty check`, `uv run pytest`.
