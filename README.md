# Jaunt

Jaunt is a small Python library + CLI for **spec-driven code generation**:

- Write implementation intent as normal Python stubs decorated with `@jaunt.magic(...)`.
- Optionally write test intent as stubs decorated with `@jaunt.test(...)`.
- Jaunt generates real modules under `__generated__/` using an LLM backend (OpenAI or Anthropic).

## Quickstart (This Repo)

Prereqs: `uv` installed.

```bash
uv sync
export OPENAI_API_KEY=...   # or ANTHROPIC_API_KEY for Claude
uv run jaunt --version
```

See `docs-site/` for rendered docs, or `DOCS.md` for a plain-text walkthrough.

All examples live under `examples/`. See `examples/README.md` for the full list.

### Hackathon Demo (JWT Auth)

Headline demo: **JWT auth** (the "wow gap" example: short spec, real generated glue + tests).

```bash
# Generate implementations for @jaunt.magic specs.
uv run jaunt build --root examples/jwt_auth

# Generate pytest tests for @jaunt.test specs and run them.
PYTHONPATH=examples/jwt_auth/src uv run jaunt test --root examples/jwt_auth
```

## Auto-Generate PyPI Skills (Build)

`jaunt build` includes a best-effort pre-build step that auto-generates “skills” for external libraries your project imports and injects them into the build prompt.

What happens:

- Scan `paths.source_roots` for `import ...` / `from ... import ...` (ignores stdlib, internal modules, and relative imports).
- Resolve imports to installed PyPI distributions + versions from the current environment.
- Ensure a skill exists per distribution at:
  - `<project_root>/.agents/skills/<dist-normalized>/SKILL.md`
- If missing/outdated, fetch the exact PyPI README for `<dist>==<version>` and generate `SKILL.md` using the configured LLM provider.
- Inject the concatenated skills text into the build LLM prompt.

Overwrite rules:

- Jaunt only overwrites a skill if it was previously Jaunt-generated (it has a `<!-- jaunt:skill=pypi ... -->` header) and the installed version changed.
- If the header is missing, the file is treated as user-managed and will never be overwritten.

Failure mode: warnings to stderr, and the build continues without missing skills.

## Docs Site (Fumadocs)

The repository includes a Fumadocs (Next.js) documentation site under `docs-site/`.

```bash
cd docs-site
npm run dev
```

## Dev

```bash
uv run ruff check .
uv run ty check
uv run pytest
```
