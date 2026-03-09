# Rich Tic-Tac-Toe (Jaunt Example)

This example is intentionally ambitious. It exercises:

- the Aider-backed Jaunt runtime
- `gpt-5.4` with `reasoning_effort = "high"`
- an explicit user-managed Rich skill built through `jaunt skill build`
- a multi-module spec graph
- a Rich-rendered terminal UI on top of pure minimax game logic

## Workflow

From the repo root:

```bash
uv sync --extra aider

# Build the checked-in Rich skill scaffold into a real user skill first.
uv run jaunt skill build --root examples/rich_tictactoe rich

# Generate the implementation and tests.
uv run jaunt build --root examples/rich_tictactoe
uv run jaunt test --root examples/rich_tictactoe

# Run the app after build.
PYTHONPATH=examples/rich_tictactoe/src uv run python -m tictactoe_demo
```

## What The Example Covers

- Human vs optimal AI.
- Deterministic minimax tie-breaking.
- Rich tables, panels, text styling, and prompt-driven interaction.
- Scriptable terminal flow through an injectable input function so the UI can be tested.

## Skills Proof

After running `jaunt skill build rich`, this file should contain user-authored Rich guidance:

`examples/rich_tictactoe/.agents/skills/rich/SKILL.md`

After build, generated code should appear under:

- `examples/rich_tictactoe/src/tictactoe_demo/__generated__/`
- `examples/rich_tictactoe/tests/__generated__/`
