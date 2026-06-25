"""Interactive CLI player for Ultimate Tic-Tac-Toe.

Tests jaunt.preserve to keep hand-written code alongside generated code.
"""
from __future__ import annotations

import jaunt
from uttt.game import UltimateTTT


@jaunt.magic()
class CLIPlayer:
    """An interactive command-line player interface for Ultimate Tic-Tac-Toe.

    Supports both human-vs-human and human-vs-AI modes. Uses UltimateTTT for
    game state and optionally uses the best_move AI from uttt.ai.

    State:
      - game: the UltimateTTT instance being played
      - mode: 'hvh' (human vs human) or 'hva' (human vs AI)
      - ai_player: 'X' or 'O' when mode='hva', else None
      - ai_depth: search depth for AI (default 3)

    Constructor:
      CLIPlayer(mode='hvh', ai_player='O', ai_depth=3)
    """

    @jaunt.preserve
    def run(self) -> None:
        """Run the game loop (hand-written: just a stub for demo purposes)."""
        print(self.game.display())
        print("CLIPlayer.run() is hand-preserved — game loop not implemented.")

    def parse_move(self, user_input: str) -> tuple[int, int]:
        """Parse a user move from a string like '3 5' or '3,5' into (board_idx, cell_idx).

        Accepts space-separated or comma-separated integers.
        Raises ValueError if the input cannot be parsed or indices are out of range.
        """
        ...

    def prompt_human_move(self) -> tuple[int, int]:
        """Print the current board, prompt the human player for a move, parse and return it.

        Keeps prompting until a syntactically valid (board_idx, cell_idx) pair is entered.
        Does NOT validate legality — that is done by game.move().
        Shows the active_board constraint in the prompt if active_board is set.
        """
        ...
