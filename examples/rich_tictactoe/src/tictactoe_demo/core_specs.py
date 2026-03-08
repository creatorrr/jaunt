"""
Rich Tic-Tac-Toe core logic.

This module owns the pure game rules and the optimal minimax AI.
The Rich UI lives in `tui_specs.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import jaunt


class Mark(StrEnum):
    X = "X"
    O = "O"


WIN_LINES: tuple[tuple[int, int, int], ...] = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)

# Deterministic tie-break order for equally optimal moves.
PREFERRED_AI_MOVES: tuple[int, ...] = (5, 1, 3, 7, 9, 2, 4, 6, 8)


@dataclass(frozen=True)
class GameState:
    """
    Immutable game state for a single tic-tac-toe round.

    board:
      nine cells in row-major order, each one of "X", "O", or " ".
    human_mark / ai_mark:
      which side each participant controls.
    next_mark:
      whose turn it is right now.
    winner:
      winning mark if the game is already over.
    is_draw:
      True only when the board is full and there is no winner.
    """

    board: tuple[str, ...]
    human_mark: Mark
    ai_mark: Mark
    next_mark: Mark
    winner: Mark | None = None
    is_draw: bool = False


@jaunt.magic()
def new_game(human_mark: Mark) -> GameState:
    """
    Create a brand-new 3x3 tic-tac-toe game.

    Requirements:
    - Validate that `human_mark` is either Mark.X or Mark.O.
    - The AI mark is the other side.
    - The board starts as nine spaces: (" ", ..., " ").
    - X always moves first, so `next_mark` must start as Mark.X.
    - `winner` starts as None and `is_draw` starts as False.
    - Return a frozen GameState without mutating any shared globals.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic()
def available_moves(state: GameState) -> tuple[int, ...]:
    """
    Return the currently open move numbers in ascending order.

    Conventions:
    - Move numbers are 1-based and map row-major from top-left to bottom-right.
    - Example: top-left is 1, center is 5, bottom-right is 9.
    - Only cells containing a single space " " are available.
    - If the game is already over, return an empty tuple.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic()
def winner_for_board(board: tuple[str, ...]) -> Mark | None:
    """
    Return the winning mark for a 9-cell board, or None if there is no winner.

    Rules:
    - Validate the board has exactly 9 cells.
    - Ignore lines made of blank spaces.
    - Use WIN_LINES for line membership.
    - Return Mark.X or Mark.O if that mark has a completed line.
    - If there is no completed line, return None.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=[available_moves, winner_for_board])
def apply_human_move(state: GameState, move: int) -> GameState:
    """
    Apply a single human move and return the next immutable state.

    Rules:
    - Reject moves if the game is already over.
    - Reject moves when it is not the human's turn.
    - `move` must be in the current available moves; otherwise raise ValueError.
    - Write the human mark into the chosen cell and leave all other cells unchanged.
    - Recompute `winner` and `is_draw`.
    - If the game continues, switch `next_mark` to the AI mark.
    - Never mutate the incoming GameState.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=[available_moves, winner_for_board])
def best_ai_move(state: GameState) -> int:
    """
    Return the AI's optimal move using full minimax search.

    Requirements:
    - Reject calls if the game is already over.
    - Reject calls when it is not the AI's turn.
    - Search the full game tree; the AI must never lose with perfect play.
    - Maximize for the AI and minimize for the human.
    - Prefer a forced win over a draw, and a draw over a forced loss.
    - When multiple moves have the same minimax score, tie-break using
      PREFERRED_AI_MOVES exactly in this order: 5, 1, 3, 7, 9, 2, 4, 6, 8.
    - Return the chosen move number as a 1-based cell index.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=[best_ai_move, winner_for_board])
def apply_ai_move(state: GameState) -> GameState:
    """
    Compute and apply the AI's move, returning the next immutable state.

    Rules:
    - Reject calls if the game is already over.
    - Reject calls when it is not the AI's turn.
    - Use best_ai_move(state) to choose the move.
    - Recompute `winner` and `is_draw`.
    - If the game continues, switch `next_mark` back to the human mark.
    - Never mutate the incoming GameState.
    """
    raise RuntimeError("spec stub (generated at build time)")
