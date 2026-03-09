# Jaunt Examples

All example projects live here. Each subfolder is a standalone Jaunt project with its own `jaunt.toml`, spec stubs, and tests.

**Important:** running these will call a language model API and spend tokens (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY` must be set, matching `llm.provider` in `jaunt.toml`).

## Examples

### Hackathon Demos

| Shortcut   | Directory          | Description                                        |
| ---------- | ------------------ | -------------------------------------------------- |
| `jwt`      | `jwt_auth/`        | HS256 JWT signing, verification, rotation (Pydantic) |
| `markdown` | `markdown_render/` | State-machine Markdown parser + escaping           |
| `limiter`  | `rate_limiter/`    | Sliding-window rate limiter with clock injection   |
| `csv`      | `csv_parser/`      | CSV coercion with strict vs lenient modes          |
| `diff`     | `diff_engine/`     | Text diff engine                                   |
| `expr`     | `expr_eval/`       | Expression evaluator                               |
| `tictactoe`| `rich_tictactoe/`  | Rich TUI Tic-Tac-Toe vs optimal minimax AI         |

### Classic Demos

| Shortcut   | Directory                  | Description                        |
| ---------- | -------------------------- | ---------------------------------- |
| `slugify`  | `01_slugify/`              | Unicode-aware URL slugification    |
| `lru`      | `02_lru_cache/`            | LRU cache implementation           |
| `dice`     | `03_dice_roller/`          | Dice expression parser + roller    |
| `pydantic` | `04_pydantic_validation/`  | Pydantic model validation          |
| `taskboard`| `05_task_board/`           | Per-method `@magic` on a service class |

### Minimal

| Shortcut | Directory   | Description                             |
| -------- | ----------- | --------------------------------------- |
| `toy`    | `toy_app/`  | Tiny email-normalisation consumer project |

## Quick Start

From the repo root:

```bash
uv sync
export OPENAI_API_KEY=...   # or ANTHROPIC_API_KEY for Claude

# Run any example via the runner:
.venv/bin/python examples/run_example.py jwt test
.venv/bin/python examples/run_example.py slugify build
.venv/bin/python examples/run_example.py csv build --force
```

The `tictactoe` example is the heavy Aider demo and has an extra prep step:

```bash
uv sync --extra aider
uv run jaunt skill build --root examples/rich_tictactoe rich
.venv/bin/python examples/run_example.py tictactoe build
PYTHONPATH=examples/rich_tictactoe/src uv run python -m tictactoe_demo
.venv/bin/python examples/run_example.py tictactoe test
```

For Aider-backed examples, parallelism is best when your API key uses the
provider's canonical env var name (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or
`CEREBRAS_API_KEY`). If you point `llm.api_key_env` at a custom name, Jaunt
currently remaps it through a global lock during Aider runs, which keeps auth
correct but serializes those tasks.

On-the-fly demo (creates a temp project, runs build + test):

```bash
.venv/bin/python examples/demo_on_the_fly.py --test --keep
```

## Output Locations

Generated outputs are written inside each example project:

- `src/<pkg>/__generated__/...` (implementations)
- `tests/__generated__/...` (pytest tests)
- `.agents/skills/**/SKILL.md` (auto-generated PyPI skills, if build runs with API key)

Review the generated code before relying on it in real projects.
