# PROMPT-100: Jaunt Examples Runner (Hackathon Demos)

Repo: `/Users/ishitajindal/Documents/jaunt`

## Objective
Make `/Users/ishitajindal/Documents/jaunt/jaunt-examples/` demoable with consistent commands and minimal friction:

1. A top-level `run_example.py` that can `build` and `test` each example project.
2. A concise `jaunt-examples/README.md` that sells the “spec is 10 lines, implementation is 80+” gap.
3. A small `_dotenv.py` helper (copied/adjusted from `/Users/ishitajindal/Documents/jaunt/examples/_dotenv.py`) so demos fail fast if the key is missing.

This runner is for hackathon demos. It is OK if it shells out to `python -m jaunt ...` and prints paths for the audience.

## Owned Files (edit only these)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/README.md` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/run_example.py` (new)
- `/Users/ishitajindal/Documents/jaunt/jaunt-examples/_dotenv.py` (new)

## Inputs / Context
Example mapping (project_dir, package):
- `jwt` -> `jaunt-examples/jwt_auth`, `jwt_demo`
- `markdown` -> `jaunt-examples/markdown_render`, `md_demo`
- `limiter` -> `jaunt-examples/rate_limiter`, `limiter_demo`
- `csv` -> `jaunt-examples/csv_parser`, `csv_demo`

All examples are “consumer projects” that run Jaunt via `python -m jaunt` with `--root`.

## Deliverables

### 1) `jaunt-examples/_dotenv.py`
Copy/adjust from `/Users/ishitajindal/Documents/jaunt/examples/_dotenv.py`:
- Provide `ensure_openai_key(env: dict[str, str], repo_root: Path) -> dict[str, str]`
- Behavior:
  - If `OPENAI_API_KEY` is already in env and non-empty, return env unchanged.
  - Else, if `<repo_root>/.env` exists, load it and set `OPENAI_API_KEY` if present.
  - If still missing, raise `SystemExit` with a clear message about setting `OPENAI_API_KEY`.

### 2) `jaunt-examples/run_example.py`
Implement a CLI runner similar to `/Users/ishitajindal/Documents/jaunt/examples/run_example.py`.

Requirements:
- `argparse` interface:
  - positional `example` in `{jwt, markdown, limiter, csv}`
  - subcommands: `build`, `test`
  - `--force` for both
  - `test` supports `--no-run` (skip pytest)
- Ensure Jaunt resolves from repo `src/`:
  - inject `<repo_root>/src` into `PYTHONPATH` for subprocesses
- Use `ensure_openai_key(...)` for env setup.
- Commands to run:
  - build: `[sys.executable, "-m", "jaunt", "build", "--root", <project_dir>]` (+ `--force`)
  - test: `[sys.executable, "-m", "jaunt", "test", "--root", <project_dir>]` (+ `--force`, `--no-run`)
- After successful runs, print:
  - generated implementations path: `<project_dir>/src/<pkg>/__generated__/`
  - generated tests path: `<project_dir>/tests/__generated__/`
  - generated skills path: `<project_dir>/.agents/skills/`

### 3) `jaunt-examples/README.md`
Write a short, punchy README:
- One paragraph on the “wow gap” (spec short, implementation annoying).
- List the 4 examples:
  - JWT auth: base64/HMAC/expiry checks; `pydantic` triggers skills
  - Markdown renderer: state-machine parsing + escaping
  - Rate limiter: sliding window + pruning + clock injection
  - CSV parser: coercion + strict/lenient modes
- Run commands (from repo root):
  - `.venv/bin/python jaunt-examples/run_example.py jwt test`
  - `.venv/bin/python jaunt-examples/run_example.py markdown build`
  - etc
- Call out:
  - running calls OpenAI (spends tokens)
  - skills are created under `<example_root>/.agents/skills/**/SKILL.md`

## Quality Gates
Run these from repo root:
```bash
.venv/bin/python -m compileall jaunt-examples/run_example.py
.venv/bin/python -m compileall jaunt-examples/*/src jaunt-examples/*/tests
```

## Constraints
- Keep `run_example.py` simple and stable. No third-party deps.
- Do not modify any files outside the Owned Files list.

