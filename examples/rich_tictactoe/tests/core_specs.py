from __future__ import annotations

import jaunt
from tictactoe_demo.core_specs import (
    apply_ai_move,
    apply_human_move,
    available_moves,
    best_ai_move,
    new_game,
    winner_for_board,
)


@jaunt.test(targets=[new_game, available_moves])
def test_new_game_initial_state() -> None:
    """
    Test-authoring rules:
    - Import `Mark` directly from `tictactoe_demo.core_specs`.
    - Do not inspect `new_game.__globals__` or any wrapper internals.
    - The GameState turn field is named `next_mark`.

    Verify:
    - new_game(Mark.X) creates an empty board with nine blank cells
    - the AI mark is Mark.O
    - X moves first
    - winner is None
    - is_draw is False
    - available_moves(new_game(...)) returns (1, 2, 3, 4, 5, 6, 7, 8, 9)
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test(targets=[winner_for_board])
def test_winner_for_board_detects_rows_columns_and_diagonals() -> None:
    """
    Test-authoring rules:
    - Import `Mark` directly from `tictactoe_demo.core_specs`.
    - Use nine-cell boards only, except for one explicit ValueError case.

    Verify:
    - X wins the top row
    - O wins the middle column
    - X wins the main diagonal
    - a board with no completed line returns None
    - a board of the wrong length raises ValueError
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test(targets=[apply_human_move])
def test_apply_human_move_rejects_invalid_and_occupied_moves() -> None:
    """
    Test-authoring rules:
    - Import `Mark` directly from `tictactoe_demo.core_specs`.
    - The GameState turn field is `next_mark`, not `next_turn`.

    Verify:
    - move 0 raises ValueError
    - move 10 raises ValueError
    - moving onto an occupied cell raises ValueError
    - trying to move when it is not the human's turn raises ValueError
    - trying to move after the game is already won or drawn raises ValueError
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test(targets=[apply_human_move])
def test_apply_human_move_updates_turn_and_terminal_flags() -> None:
    """
    Test-authoring rules:
    - Import `Mark` directly from `tictactoe_demo.core_specs`.
    - Use the field name `next_mark`.

    Verify:
    - a legal move writes the human mark into the chosen cell
    - if the move does not end the game, next_mark flips to the AI mark
    - if the move completes a winning line, winner is set and is_draw is False
    - if the move fills the final cell without a winner, is_draw is True
    - the original GameState is unchanged
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test(targets=[best_ai_move])
def test_best_ai_move_prefers_center_and_respects_tie_break_order() -> None:
    """
    Test-authoring rules:
    - Import `Mark` directly from `tictactoe_demo.core_specs`.
    - The deterministic tie-break order is exactly: 5, 1, 3, 7, 9, 2, 4, 6, 8.

    Verify:
    - on an empty board where the AI opens, best_ai_move(...) returns 5
    - when center is unavailable and multiple moves are equally optimal, the AI
      picks the earliest move from the exact tie-break order
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test(targets=[best_ai_move])
def test_best_ai_move_takes_wins_and_blocks_forced_losses() -> None:
    """
    Test-authoring rules:
    - Import `Mark` directly from `tictactoe_demo.core_specs`.

    Verify:
    - if the AI has an immediate winning move, it takes it
    - if the human has an immediate winning threat, the AI blocks it
    - the returned move is a 1-based cell number
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test(targets=[apply_ai_move])
def test_apply_ai_move_uses_best_ai_move_and_keeps_state_immutable() -> None:
    """
    Test-authoring rules:
    - Import `Mark` directly from `tictactoe_demo.core_specs`.
    - The field name is `next_mark`, not `next_turn`.

    Verify:
    - apply_ai_move uses best_ai_move internally
    - it writes exactly one new AI mark
    - it never overwrites an occupied cell
    - if the move ends the game, winner or is_draw is updated correctly
    - otherwise, next_mark flips back to the human mark
    - the original GameState is unchanged
    """
    raise AssertionError("spec stub (generated at test time)")
