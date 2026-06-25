"""Ultimate Tic-Tac-Toe game implementation specs."""
from __future__ import annotations

import jaunt


@jaunt.magic()
class UltimateTTT:
    """A complete implementation of Ultimate Tic-Tac-Toe.

    The board is a 3x3 grid of nine small 3x3 tic-tac-toe boards (local boards).
    Positions on both the meta-board and local boards are 0-indexed from 0..8,
    where index i corresponds to row i//3, col i%3.

    State:
      - boards: list of 9 local boards, each a list of 9 cells. Cell values are
        'X', 'O', or None (empty).
      - meta: list of 9 cells representing the meta-board (who won each local board).
        Values are 'X', 'O', or None.
      - current_player: 'X' or 'O' (X always goes first).
      - active_board: index 0..8 of the local board the next player MUST play in,
        or None if the player may choose any non-finished board.
      - winner: 'X', 'O', 'draw', or None if game is still ongoing.

    Rules:
      1. X goes first. Players alternate.
      2. A move is (board_idx, cell_idx): play in cell_idx of local board board_idx.
      3. The cell_idx you play determines which local board your opponent must play in
         next (active_board = cell_idx). This is the core mechanic.
      4. If the forced next board is already won or full (no empty cells), the opponent
         may play in ANY local board that is not won and not full.
      5. Winning a local board (three in a row: rows, columns, or diagonals) claims
         that cell on the meta-board for the winning player.
      6. Win three claimed cells in a row on the meta-board to win the game.
      7. If all local boards are complete (won or full) and no player has won the
         meta-board, the result is a draw.

    Constructor:
      UltimateTTT() — creates a fresh game with all boards empty, X to move,
      active_board=None (first player chooses freely).
    """

    def move(self, board_idx: int, cell_idx: int) -> None:
        """Make a move: play in cell_idx of local board board_idx.

        Validates that:
          - The game is not over.
          - board_idx is 0..8.
          - cell_idx is 0..8.
          - The chosen board is the active_board (or active_board is None and the
            board is not finished).
          - The chosen cell is empty.

        After placing the piece:
          1. Check if the local board is now won; if so update meta[board_idx].
          2. Determine next active_board = cell_idx. If that board is already won
             or full, set active_board = None (free choice).
          3. Check if the meta-board is now won; if so set self.winner.
          4. Check if all boards are done (draw condition); if so set self.winner = 'draw'.
          5. Switch current_player.

        Raises ValueError with a descriptive message for any invalid move.
        """
        ...

    def legal_moves(self) -> list[tuple[int, int]]:
        """Return all legal (board_idx, cell_idx) moves for the current player.

        Returns an empty list if the game is over.

        A move (b, c) is legal if:
          - The game is not over.
          - Board b is active (b == active_board, or active_board is None and
            board b is not won and not full).
          - Cell c is empty in board b.
        """
        ...

    def is_board_won(self, board_idx: int) -> str | None:
        """Return 'X' or 'O' if local board board_idx has been won, else None.

        Checks all 8 winning lines (3 rows, 3 cols, 2 diagonals).
        """
        ...

    def is_board_full(self, board_idx: int) -> bool:
        """Return True if local board board_idx has no empty cells, else False."""
        ...

    def is_board_finished(self, board_idx: int) -> bool:
        """Return True if local board board_idx is won or full, else False."""
        ...

    def display(self) -> str:
        """Return a human-readable ASCII string showing the full game state.

        Format: 9 local boards arranged in a 3x3 grid, with board separators.
        Mark the active board clearly. Show whose turn it is and the winner if any.
        Use '.' for empty cells, 'X' for X, 'O' for O.
        When a local board has been won, show 'W' in its center cell to indicate
        the winner on that board (use the winner's letter, e.g. 'X' or 'O', placed
        in the center position of the local board's ASCII representation).
        """
        ...
