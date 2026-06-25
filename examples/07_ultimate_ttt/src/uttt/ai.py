"""AI player spec for Ultimate Tic-Tac-Toe using minimax with alpha-beta pruning."""
from __future__ import annotations

import jaunt
from uttt.game import UltimateTTT


@jaunt.magic()
def best_move(game: UltimateTTT, depth: int = 4) -> tuple[int, int]:
    """Return the best move for the current player using minimax with alpha-beta pruning.

    Evaluates the game tree to the given depth. Uses a heuristic evaluation function
    when the depth limit is reached (not a terminal state).

    Heuristic scoring (from current_player's perspective):
      - Winning the game: +10000
      - Losing the game: -10000
      - Draw: 0
      - For each local board not yet won: count lines where current_player has
        cells and opponent has none. Each such line scores +1 per current_player
        piece in it. Lines where opponent has pieces score -1 per opponent piece.
      - Winning a local board: +100. Opponent winning a local board: -100.
      - Owning the center of the meta-board (local board 4): +10.
      - Owning a corner of the meta-board (boards 0,2,6,8): +5 each.

    Args:
        game: The current game state (not mutated).
        depth: The maximum search depth (default 4, higher is slower but stronger).

    Returns:
        A (board_idx, cell_idx) tuple for the best move found.
        If there is only one legal move, return it immediately without searching.

    Raises:
        ValueError if there are no legal moves (game over or impossible state).
    """
    ...


@jaunt.magic()
def evaluate(game: UltimateTTT, for_player: str) -> float:
    """Compute a heuristic score of the game state for for_player.

    Score from for_player's perspective:
      - Terminal states: +10000 if for_player won, -10000 if opponent won, 0 for draw.
      - Non-terminal: sum of local board scores plus meta-board position scores.

    Local board scores:
      - for each unfinished local board: for each of the 8 winning lines:
        - if the line has only for_player's pieces (and some empty): +count of for_player pieces
        - if the line has only opponent's pieces (and some empty): -count of opponent pieces
      - Winning a local board: +100 for for_player, -100 if opponent won.

    Meta-board position bonus:
      - Center board (index 4) won by for_player: +50
      - Corner boards (0,2,6,8) won by for_player: +20 each
      - Center board (index 4) won by opponent: -50
      - Corner boards won by opponent: -20 each

    Args:
        game: The game state to evaluate.
        for_player: 'X' or 'O'.

    Returns:
        A float score; higher is better for for_player.
    """
    ...
