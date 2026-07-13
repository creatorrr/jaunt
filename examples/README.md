# Jaunt Examples

All example projects live here. Each subfolder is a standalone Jaunt project with its own `jaunt.toml`, spec stubs, and tests.

**Important:** running these calls the Codex API and spends tokens; the `codex` CLI must be installed and authenticated (`codex login`).

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

| Shortcut    | Directory                  | Description                                                                         |
| ----------- | -------------------------- | ----------------------------------------------------------------------------------- |
| `slugify`   | `01_slugify/`              | Unicode-aware URL slugification                                                     |
| `lru`       | `02_lru_cache/`            | LRU cache implementation                                                            |
| `dice`      | `03_dice_roller/`          | Dice expression parser + roller                                                     |
| `pydantic`  | `04_pydantic_validation/`  | Pydantic model validation                                                           |
| `taskboard` | `05_task_board/`           | Per-method `@magic` on a service class                                              |
| `whole`     | `06_whole_class/`          | Whole-class `@jaunt.magic` — game/inventory/stats                                   |
| `uttt`      | `07_ultimate_ttt/`         | Ultimate Tic-Tac-Toe: game + minimax AI + CLI; built end-to-end by a first-time agent — see its `CASE-STUDY.md` |

### Contract mode

These run `jaunt reconcile` / `jaunt check` instead of `jaunt build`; the ones
marked deterministic derive without a model or API key.

| Directory              | Description                                                                 |
| ---------------------- | --------------------------------------------------------------------------- |
| `contract_slugify/`    | Contract-mode walkthrough: strong vs deliberately weak contracts (deterministic) |
| `contract_async/`      | Async functions and fixtures under contract mode                            |
| `contract_properties/` | Hypothesis `properties` case kind: conservation, round-trips, bounds — and a bug examples can't catch (deterministic) |

### TypeScript alpha

| Directory                        | Description |
| -------------------------------- | ----------- |
| `typescript_slugify/`            | Real `gpt-5.6-sol` TypeScript generation, example/derived Vitest batteries, ordinary JS emit, and runtime demo |
| `typescript_project_references/` | Composite core/app npm workspace with a cross-project spec dependency and emitted Node demo |
| `typescript-jwt/`                | Generated JWT library with classes, held-out and contract batteries, mutation strength, packed-consumer smoke, and ejection coverage |

### Minimal

| Shortcut | Directory   | Description                             |
| -------- | ----------- | --------------------------------------- |
| `toy`    | `toy_app/`  | Tiny email-normalisation consumer project |

## Quick Start

From the repo root:

```bash
uv sync

# One-time setup: install and authenticate the Codex CLI.
codex login

# Run any example via the runner:
.venv/bin/python examples/run_example.py jwt test
.venv/bin/python examples/run_example.py slugify build
.venv/bin/python examples/run_example.py csv build --force
```

The `tictactoe` example has an extra prep step to build the Rich user skill first:

```bash
uv run jaunt skill build --root examples/rich_tictactoe rich
.venv/bin/python examples/run_example.py tictactoe build
PYTHONPATH=examples/rich_tictactoe/src uv run python -m tictactoe_demo
.venv/bin/python examples/run_example.py tictactoe test
```

On-the-fly demo (creates a temp project, runs build + test):

```bash
.venv/bin/python examples/demo_on_the_fly.py --test --keep
```

## Output Locations

Generated outputs are written inside each example project:

- `src/<pkg>/__generated__/...` (implementations)
- `tests/__generated__/...` (pytest tests)
- `.agents/skills/**/SKILL.md` (auto-generated PyPI skills)

Review the generated code before relying on it in real projects.
