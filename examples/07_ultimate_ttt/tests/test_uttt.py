"""Test specs for Ultimate Tic-Tac-Toe."""
from __future__ import annotations

import jaunt
from uttt.game import UltimateTTT


@jaunt.test(targets=UltimateTTT)
def test_initial_state():
    """A fresh UltimateTTT game should have correct initial state.

    - current_player is 'X'
    - winner is None
    - active_board is None (first player can choose any board)
    - all 9 local boards are empty (9 None cells each)
    - meta is all None
    - legal_moves() returns all 81 possible moves (9 boards × 9 cells)
    """
    ...


@jaunt.test(targets=UltimateTTT)
def test_first_move_sets_active_board():
    """After the first move, active_board is determined by the cell played.

    Example: X plays board=0, cell=4 (center). Then active_board must be 4.
    After this move, current_player is 'O'. The cell at boards[0][4] is 'X'.
    legal_moves() must only return moves in board 4 (9 moves).
    """
    ...


@jaunt.test(targets=UltimateTTT)
def test_forced_board_redirect_when_won():
    """When forced board is already won, active_board becomes None.

    Set up a scenario where:
    - Local board 4 is already won by X.
    - A player is about to play a cell whose index is 4 (forcing play into board 4).
    After that move, active_board should be None because board 4 is won,
    and legal_moves() should return moves from all unfinished boards.
    """
    ...


@jaunt.test(targets=UltimateTTT)
def test_forced_board_redirect_when_full():
    """When forced board is full (but not won), active_board becomes None.

    Fill local board 7 completely without anyone winning it (a drawn local board).
    Then make a move in some other board with cell_idx=7. After that move,
    active_board should be None, and legal_moves() should return moves from
    all non-finished boards (which excludes board 7).
    """
    ...


@jaunt.test(targets=UltimateTTT)
def test_win_local_board():
    """Winning a local board updates the meta-board.

    Play X in board 0 at cells 0, 1, 2 (top row of board 0) with valid interleaved
    O moves on other boards. After X completes the row in board 0:
    - meta[0] should be 'X'.
    - is_board_won(0) should return 'X'.
    - winner should still be None (game not over yet).
    """
    ...


@jaunt.test(targets=UltimateTTT)
def test_win_game_via_meta_board():
    """Winning three local boards in a row wins the overall game.

    Simulate X winning local boards 0, 4, and 8 (diagonal on meta-board).
    After X wins board 8, self.winner should be 'X'.
    legal_moves() should return [] once the game is won.
    """
    ...


@jaunt.test(targets=UltimateTTT)
def test_invalid_move_wrong_board():
    """move() raises ValueError when playing in a board that is not active.

    If active_board is 3, playing in board 5 should raise ValueError.
    """
    ...


@jaunt.test(targets=UltimateTTT)
def test_invalid_move_occupied_cell():
    """move() raises ValueError when playing in an already occupied cell.

    Play X at board=0, cell=0. Then attempt to play O at board=0, cell=0.
    (Assuming active_board directs back to board 0.)
    This should raise ValueError.
    """
    ...


@jaunt.test(targets=UltimateTTT)
def test_invalid_move_game_over():
    """move() raises ValueError when the game is already over.

    After the game has been won, any further move() call should raise ValueError.
    """
    ...


@jaunt.test(targets=UltimateTTT)
def test_draw_detection():
    """The game detects a draw when all local boards are complete and no one won meta.

    Fill all 9 local boards such that no player wins 3 in a row on the meta-board.
    After filling the last cell, winner should be 'draw'.
    """
    ...
