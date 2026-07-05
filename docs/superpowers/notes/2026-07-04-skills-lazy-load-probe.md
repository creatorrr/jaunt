# Skills lazy-load probe (finding 19 verification)

Date: 2026-07-04
Scope: one-off empirical check during jaunt 1.5 (spec §1b). Not a shipped
feature; no test-suite footprint.

## Question

Adoption feedback (finding 19) reported that seeded skills dominate the build
context — `context_stats.skills_workspace` measured ~95% of bytes. But that
number is worst-case *seeded-on-disk* exposure, not consumption. Does codex
actually read every SKILL.md body up front (eager), or only open the ones that
apply to the task (lazy)? The answer decides whether jaunt 1.6 needs
per-module skill pruning (held as a non-goal unless the reads are eager).

## Method

Seeded a faithful build workspace and ran a single real `codex exec` against
it under a read-only sandbox with the `--json` event stream captured.

- Workspace: `/tmp/probe-ws`, mirroring the builder's temp layout
  (`codex_backend.py:358-391`): `_context/spec_0.py` (the jwt_auth specs) and a
  `.agents/skills/` seeded via `seed_skills_into_workspace(project_root=None,
  builtin_names=DEFAULT_BUILTIN_SKILLS)` — **13 builtin skills, 32,434 bytes**
  total (asyncpg, dbos, descope, fastmcp, openai, pydantic, pydantic-ai,
  pytest, ruff, spacy, starlette, ty, uv).
- Prompt: the real build-prompt blocks (preamble, "write module …", "spec
  stubs are the contract", the "skills are available in `.agents/skills/`;
  consult them when they apply" line, implementer-role block), with a trailing
  read-only instruction to work out the implementation and then report which
  SKILL.md files it opened and which it skipped.
- Invocation: `codex exec --json --sandbox read-only`, model `gpt-5.5`,
  `model_reasoning_effort=high`. Raw events in `/tmp/probe-events.jsonl` (30
  events, not committed).

The jwt_auth task is a good stress case: it uses pydantic (a seeded skill) and
touches nothing the other 12 skills cover, so eager vs. lazy is easy to tell
apart.

## Raw counts

From the `command_execution` items in the event stream:

- `1` listing pass — `rg --files _context .agents/skills` (enumerates skill
  paths; does not read bodies).
- `3` SKILL.md bodies opened, all directly relevant to the task:
  - `.agents/skills/pydantic/SKILL.md` (the spec's `Claims(BaseModel)`)
  - `.agents/skills/ruff/SKILL.md`
  - `.agents/skills/ty/SKILL.md`
- `10` SKILL.md bodies **never opened**. In its final message codex listed each
  skipped skill with a one-line reason (e.g. "asyncpg: no database code";
  "openai: no OpenAI SDK usage"; "pytest: user said not to run or write
  tests"), confirming the skips were deliberate, not accidental.

Read ratio: **3 / 13 bodies (≈23%)**. Bytes actually read were a small
fraction of the 32 KB seeded on disk.

## Conclusion

**Lazy.** Codex lists the skill directory once, then opens only the SKILL.md
bodies whose subject matches the task, and explains its skips. It does not
eagerly slurp skill bodies. The finding-19 "skills are ~95% of context" figure
is a reporting artifact of measuring seeded-on-disk bytes rather than consumed
tokens — exactly the confusion the 1.5 `skills_workspace_seeded` relabel and
its "available to the agent, not necessarily read" doc note are meant to fix.

Caveat: single trial, one task shape, 13 builtin skills (a full jwt_auth build
also auto-generates a few PyPI skills, but those overlap the builtins and do
not change the eager-vs-lazy signal). The direction of the result is
unambiguous even so.

## Recommendation for 1.6

Do **not** design per-module skill pruning/filtering machinery. The spec gated
that work on the probe showing eager reads; the reads are lazy, so the machinery
would add config surface and staleness risk for no measured token savings. If a
future adopter still reports skill-driven cost, re-run this probe on their real
task mix before revisiting — the honest `skills_workspace_seeded` label should
already resolve the perception half of the complaint.
