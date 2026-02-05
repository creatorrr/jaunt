# Jaunt Examples (Real OpenAI API Calls)

These examples are small Jaunt projects you can run end-to-end:

- `@jaunt.magic` specs in `src/<pkg>/specs.py` generate implementations under `src/<pkg>/__generated__/...`
- `@jaunt.test` specs in `tests/` generate real pytest tests under `tests/__generated__/...`

Important: running these will call the OpenAI API and spend tokens.

## Prereqs

- `OPENAI_API_KEY` set in your environment, or add it to `<repo_root>/.env`.
- This repo's dev environment set up (for example: `uv sync`).

## Quick Start

From the repo root:

```bash
.venv/bin/python examples/run_example.py slugify test
.venv/bin/python examples/run_example.py lru test
.venv/bin/python examples/run_example.py dice test
```

On-the-fly demo (creates a temp project, runs `jaunt build`, optionally `jaunt test`):

```bash
.venv/bin/python examples/demo_on_the_fly.py --test --keep
```

## Output Locations

Generated outputs are written inside each example project:

- `src/<pkg>/__generated__/...` (implementations)
- `tests/__generated__/...` (pytest tests)

Review the generated code before relying on it in real projects.

