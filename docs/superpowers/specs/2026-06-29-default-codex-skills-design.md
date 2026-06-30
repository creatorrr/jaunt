# Default Codex Builder Skills — Design

**Date:** 2026-06-29
**Status:** Proposed
**Scope:** Jaunt framework (affects every project's Codex builder by default)

## Problem

Jaunt's Codex builder should ship with a curated set of "batteries-included"
skills for the libraries and dev tooling its generated code most commonly
touches, so the generator writes idiomatic, conformant code without each project
having to discover or generate those skills first.

The default set (13 skills):

- **Libraries:** `pydantic`, `pydantic-ai`, `descope`, `starlette`, `fastmcp`,
  `dbos`, `openai`, `spacy`, `asyncpg`
- **Conformance / tooling:** `ruff`, `ty`, `pytest`, `uv`

## Key Finding (architecture)

`codex exec` natively discovers the Agent-Skills protocol: it reads `SKILL.md`
files (YAML `name`/`description` frontmatter + progressive disclosure) from
`.agents/skills/` **and** `.codex/skills/` relative to its working directory, in
addition to global `~/.codex/skills/`. Verified empirically against
`codex-cli 0.142.4`: skills seeded into a workspace's `.agents/skills/` appear
in the model's available-skills list.

**The gap:** `jaunt build` does not run Codex in the project root.
`CodexBackend.generate_module` creates a throwaway `TemporaryDirectory`, seeds
only the target file + `_context/`, and runs `codex exec -C <tmp>`. So Codex
looks for `.agents/skills/` *inside the temp workspace*, which jaunt does not
populate. Today jaunt bridges this by injecting full skill text into the build
prompt (`skills_block`, capped at `max_chars_per_skill`).

To make Codex discover skills natively, **jaunt must seed skill directories into
the temp workspace.** This also lets us drop the prompt-text injection: only
each skill's `name`/`description` sits in context until Codex chooses to open
the body (progressive disclosure), so the 13 defaults cost ~13 description lines
instead of up to ~24k tokens.

## Decisions (locked)

1. **Hybrid authoring.** Hand-write the 4 tooling skills; draft the 9 library
   skills from their PyPI READMEs (via the existing generator), then trim and
   review. All 13 ship as static bundled files.
2. **Package-only, seed into temp.** Builtin skills live inside the jaunt wheel.
   They are seeded into `<tmp>/.agents/skills/` on each build — not written into
   the user's repo. A project may override a builtin by creating a same-named
   skill in its own `.agents/skills/` (project wins).
3. **Drop full-text injection.** Remove `skills_block` from the Codex build
   prompt; rely entirely on native discovery. Because Codex is now jaunt's sole
   engine, this is a clean removal.

## Design

### Components

1. **Bundled builtin skills** — `src/jaunt/skills/builtin/<name>/SKILL.md`
   - Each file has YAML frontmatter: `name`, `description`. The `description`
     is tuned for progressive disclosure ("Use when generating code that imports
     `<lib>` …" / "Use whenever writing Python that must pass `ruff check`/`ty
     check`/pytest …").
   - Packaged as wheel data (`pyproject.toml` package-data / inclusion).
   - A small registry module exposes the default skill names and resolves their
     on-disk paths inside the installed package.

2. **`SkillsConfig` additions** (`config.py`)
   - `builtin: bool = True` — seed the bundled defaults.
   - `builtin_skills: list[str] = <the 13 names>` — the default set; projects can
     trim or extend it.
   - `max_chars_per_skill` / `inject_user_skills` become legacy no-ops for the
     Codex path (kept for back-compat parsing; documented as unused). `auto`
     still controls per-project PyPI skill generation.

3. **Skill format migration** (`skills_auto.py`, `skill_manager.py`,
   `prompts/pypi_skill_system.md`)
   - Generated PyPI skills emit YAML frontmatter instead of the
     `<!-- jaunt:skill=pypi dist=… version=… -->` HTML-comment header.
   - Version tracking moves into frontmatter keys (e.g. `x-jaunt-dist`,
     `x-jaunt-version`). `_parse_generated_header` → frontmatter parser;
     `_format_generated_skill_file` writes frontmatter; `discover_all_skills`
     classifies auto-vs-user from the presence of those keys.
   - `build_skills_block` (prompt injection) is removed.
   `ensure_pypi_skills_and_block` becomes `ensure_pypi_skills` (ensures files on
   disk; returns warnings/failures, no text block).

4. **Temp-workspace seeding** (`generate/codex_backend.py`)
   - `CodexBackend.generate_module` copies skill dirs into
     `<tmp>/.agents/skills/`:
     - all enabled builtin skills from the package, then
     - the project's own `.agents/skills/` (project entries overwrite builtins
       of the same name).
   - The backend needs the project root and the enabled builtin set. Thread them
     on `ModuleSpecContext` as new fields `project_root: Path` and
     `builtin_skill_names: list[str]`, replacing the now-removed `skills_block`
     field (that is the existing channel for per-call build context, alongside
     `spec_sources`/`dependency_apis`). `skills_block` usage in `_build_prompt`
     is removed.
   - The test-generation path (`tester.py` → same backend) receives identical
     seeding for symmetry.

5. **CLI** (`cli.py`)
   - Add `--no-builtin-skills` (mirrors `--no-auto-skills`).
   - Build/test flows: ensure per-project PyPI skills on disk (unchanged trigger,
     new format), then pass enabled builtin names + project root to the backend.
     The old "compute skills_block and stuff into ctx" wiring is removed.

### Data flow (build)

```
jaunt build
  → discover specs/modules (unchanged)
  → ensure_pypi_skills(): generate/refresh project .agents/skills/* (frontmatter) [if skills.auto]
  → for each module: CodexBackend.generate_module(ctx)
        → mkdtemp; seed target + _context/
        → seed <tmp>/.agents/skills/  ← builtin package skills + project .agents/skills/
        → codex exec -C <tmp>  (Codex discovers skills natively)
        → read generated module back
```

### Error handling

- Seeding is best-effort: a failure to copy a skill dir logs a warning and does
  not fail the build (matches today's best-effort auto-skill behavior).
- Missing/garbled builtin file → skip that skill with a warning.
- A builtin name listed in `builtin_skills` but absent from the package → warn.
- Name collisions resolve project-over-builtin deterministically.

### Testing

- **Builtin packaging:** every name in the default `builtin_skills` resolves to a
  bundled `SKILL.md`; each parses as valid frontmatter with non-empty
  `name`/`description`.
- **Seeding:** `generate_module` seeds expected skill dirs into the temp
  workspace; project skills override builtins of the same name; disabled via
  `builtin=False` / `--no-builtin-skills`.
- **Format migration:** generated PyPI skills round-trip through the new
  frontmatter writer/parser; `discover_all_skills` classifies auto vs user
  correctly; staleness/version-change regeneration still triggers.
- **Prompt:** `_build_prompt` no longer contains skill bodies.
- **Backend tests** continue to mock the Codex subprocess (no API key needed).
- **One integration smoke** (gated/optional): seed a marker skill and assert
  Codex lists it — already validated manually; keep a thin guarded check.

## Out of scope (YAGNI)

- Import-gated selection of builtin skills (progressive disclosure makes idle
  skills ~free; seed all enabled defaults).
- Materializing builtins into the user's repo / a `jaunt skill sync` command.
- Per-skill enable/disable beyond the `builtin_skills` list.
- Re-tuning `max_chars_per_skill` (injection is gone).

## Authoring note

The 9 library skills are drafted from PyPI READMEs and then human-reviewed for
accuracy and concision (1–2 pages). The 4 tooling skills are written from
scratch and oriented around making generated code conform (lint/type/test/deps),
not around general CLI usage.
