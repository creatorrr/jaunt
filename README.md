# Jaunt

<div align="center">
  <img src="docs-site/public/images/tyger-blake-1794.jpg" alt="William Blake, 'The Tyger' from Songs of Experience (1794). The Metropolitan Museum of Art, Open Access." width="280" />
  <br/>
  <sub>William Blake, <em>The Tyger</em>, plate 42 from <em>Songs of Experience</em> (1794).
  <a href="https://www.metmuseum.org/art/collection/search/347983">The Metropolitan Museum of Art, Open Access.</a></sub>
</div>

<br/>

> *Tyger Tyger, burning bright,*
> *In the forests of the night;*
> *What immortal hand or eye,*
> *Could frame thy fearful symmetry?*
>
> -- William Blake, via Alfred Bester's *The Stars My Destination*

Jaunt is a small Python library + CLI for **spec-driven code generation**:

- Write implementation intent as normal Python stubs decorated with `@jaunt.magic(...)`.
- Optionally write test intent as stubs decorated with `@jaunt.test(...)`.
- Jaunt generates real modules under `__generated__/` using an LLM backend (OpenAI, Anthropic, or Cerebras).
- Async support is available for both implementation and test specs through `async def` plus the `build.async_runner` setting.

## Installation

```bash
pip install jaunt[openai]      # for OpenAI
pip install jaunt[anthropic]   # for Anthropic/Claude
pip install jaunt[cerebras]    # for Cerebras
pip install jaunt[all]         # both providers
```

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

## Eval Suite

Run the built-in eval suite against your configured backend:

```bash
uv run jaunt eval
uv run jaunt eval --model gpt-4o
uv run jaunt eval --provider anthropic --model claude-sonnet-4-5-20250929
```

Compare explicit provider/model targets:

```bash
uv run jaunt eval --compare openai:gpt-4o anthropic:claude-sonnet-4-5-20250929
```

Eval outputs are written under `.jaunt/evals/<timestamp>/`.

### Eval Results (2026-02-15 UTC)

| Run (UTC) | Mode | Target | Reasoning | Passed | Failed | Skipped | Total | Notes | Artifacts |
| --- | --- | --- | --- | ---:| ---:| ---:| ---:| --- | --- |
| 2026-02-15T21-34-58Z | single | `cerebras:gpt-oss-120b` | none | 0 | 10 | 0 | 10 | Missing `cerebras-cloud-sdk` dependency | [`examples/expr_eval/.jaunt/evals/2026-02-15T21-34-58Z`](examples/expr_eval/.jaunt/evals/2026-02-15T21-34-58Z) |
| 2026-02-15T21-35-17Z | single | `cerebras:gpt-oss-120b` | none | 0 | 10 | 0 | 10 | Cerebras `402 payment_required` quota/billing error | [`examples/expr_eval/.jaunt/evals/2026-02-15T21-35-17Z`](examples/expr_eval/.jaunt/evals/2026-02-15T21-35-17Z) |
| 2026-02-15T21-36-54Z | single | `cerebras:gpt-oss-120b` | none | 10 | 0 | 0 | 10 | All eval cases passed | [`examples/expr_eval/.jaunt/evals/2026-02-15T21-36-54Z`](examples/expr_eval/.jaunt/evals/2026-02-15T21-36-54Z) |
| 2026-02-15T22-01-24Z-custom-compare | compare | `cerebras:gpt-oss-120b` | low | 10 | 0 | 0 | 10 | All eval cases passed | [`examples/expr_eval/.jaunt/evals/2026-02-15T22-01-24Z-custom-compare`](examples/expr_eval/.jaunt/evals/2026-02-15T22-01-24Z-custom-compare) |
| 2026-02-15T22-01-24Z-custom-compare | compare | `openai:gpt-5.2` | none | 10 | 0 | 0 | 10 | All eval cases passed | [`examples/expr_eval/.jaunt/evals/2026-02-15T22-01-24Z-custom-compare`](examples/expr_eval/.jaunt/evals/2026-02-15T22-01-24Z-custom-compare) |
| 2026-02-15T22-01-24Z-custom-compare | compare | `anthropic:opus-4.6` | none | 0 | 10 | 0 | 10 | Anthropic `404 not_found_error` for model name | [`examples/expr_eval/.jaunt/evals/2026-02-15T22-01-24Z-custom-compare`](examples/expr_eval/.jaunt/evals/2026-02-15T22-01-24Z-custom-compare) |
| 2026-02-15T22-04-19Z-custom-compare | compare | `cerebras:gpt-oss-120b` | low | 10 | 0 | 0 | 10 | All eval cases passed | [`examples/expr_eval/.jaunt/evals/2026-02-15T22-04-19Z-custom-compare`](examples/expr_eval/.jaunt/evals/2026-02-15T22-04-19Z-custom-compare) |
| 2026-02-15T22-04-19Z-custom-compare | compare | `openai:gpt-5.2` | none | 10 | 0 | 0 | 10 | All eval cases passed | [`examples/expr_eval/.jaunt/evals/2026-02-15T22-04-19Z-custom-compare`](examples/expr_eval/.jaunt/evals/2026-02-15T22-04-19Z-custom-compare) |
| 2026-02-15T22-04-19Z-custom-compare | compare | `anthropic:claude-haiku-4-5` | none | 9 | 1 | 0 | 10 | One assertion failure (`example_slugify_smoke`) | [`examples/expr_eval/.jaunt/evals/2026-02-15T22-04-19Z-custom-compare`](examples/expr_eval/.jaunt/evals/2026-02-15T22-04-19Z-custom-compare) |

Prompt snapshots:

```bash
uv run pytest tests/test_prompt_snapshots.py --snapshot-update
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

## Publish to PyPI

If you keep your token in `.env` as `UV_PUBLISH_TOKEN=...`, load it into your shell first:

```bash
set -a
source .env
set +a
```

Build and validate artifacts:

```bash
uv build
uvx twine check dist/*
```

Upload to PyPI:

```bash
uv publish --check-url https://pypi.org/simple/
```

## Dev

```bash
uv run ruff check .
uv run ty check
uv run pytest
```

## Why "Jaunt"?

Named after *jaunting* -- teleportation by thought alone -- from Alfred
Bester's 1956 novel [*The Stars My Destination*](https://en.wikipedia.org/wiki/The_Stars_My_Destination)
(originally published as *Tiger! Tiger!*). You think about where you want to
be, and you're there.

Jaunt works the same way: describe your intent, and arrive at working code.

The forge-and-furnace imagery you'll find scattered through the codebase
comes from William Blake's poem "The Tyger," which Bester used as the
novel's epigraph and alternate title. The poem's vision of creation --
hammer, chain, furnace, anvil -- mirrors the act of forging code from pure
specification.
