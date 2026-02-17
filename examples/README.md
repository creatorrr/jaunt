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
