"""
Rich terminal UI for the tic-tac-toe example.

This module should use the local `rich` skill heavily when built.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import jaunt
from rich.console import Console, Group
from rich.table import Table

from .core_specs import GameState, apply_ai_move, apply_human_move, available_moves, new_game


@jaunt.magic(deps=[available_moves])
def render_board(state: GameState) -> Table:
    """
    Return a Rich Table that renders the current board.

    Requirements:
    - Render a 3x3 grid.
    - Filled cells show "X" or "O" with strong styling.
      Suggested default styling:
      - X: bold cyan
      - O: bold magenta
    - Empty cells show their move numbers 1..9 in a dim style so the player
      always knows what to type.
    - Keep the board legible in a standard 80-column terminal.
    - Do not mutate the GameState.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=[available_moves, render_board])
def render_screen(state: GameState, message: str | None = None) -> Group:
    """
    Return a composed Rich screen for the current game state.

    Screen contents:
    - A title / subtitle area describing the demo.
    - The title area must literally include the word "Rich" and "Tic-Tac-Toe".
    - The rendered board from render_board(state).
    - A status section that says whose turn it is, which side is human vs AI,
      and which move numbers are currently available.
    - The screen itself, not only the side-selection screen, must explain that
      moves are entered as 1..9 and q/quit exits.
    - If `message` is provided, show it prominently.
    - If the game is over, show a clear final banner for human win, AI win, or draw.

    Style goals:
    - Use Rich composition rather than plain strings.
    - Keep the layout polished but prompt-driven, not full-screen event driven.
    - Use normal static imports from `.core_specs`; do not use `importlib`,
      runtime loaders, or dynamic import helpers.
    - If you build an intermediate collection before calling `Group(...)`, use
      Rich's public renderable typing rather than `list[object]`.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=[new_game, apply_human_move, apply_ai_move, render_screen])
def play_cli(
    console: Console,
    input_fn: Callable[[str], str] | None = None,
) -> Literal["human", "ai", "draw", "quit"]:
    """
    Run an interactive Rich-based tic-tac-toe session.

    Behavior:
    - Use `console` for all output.
    - If `input_fn` is None, default to console.input.
    - Start by asking the player to choose X or O. Allow q/quit to exit early.
    - X always moves first. If the human chooses O, the AI should make the opening move.
    - On each human turn:
      - render the current screen
      - prompt for a move number
      - accept 1..9, q, or quit
      - on invalid or occupied input, show a visible error message and re-prompt
    - On each AI turn:
      - render the current screen
      - make the optimal AI move
      - render again with a short message describing the move
    - When the round ends, render the final result banner and prompt:
      "Play again? [y/n]"
    - `y` starts a fresh round from side selection.
    - `n` returns the final result of the just-finished round:
      "human", "ai", or "draw".
    - Quitting from any prompt returns "quit".
    - The function must be deterministic when given a deterministic input_fn.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=[play_cli])
def main() -> int:
    """
    CLI entrypoint for `python -m tictactoe_demo`.

    Requirements:
    - Construct a Console.
    - Call play_cli(console).
    - Return exit code 0 for all normal outcomes, including quit.
    - Do not swallow unexpected exceptions.
    """
    raise RuntimeError("spec stub (generated at build time)")
