from __future__ import annotations

import jaunt


@jaunt.test()
def test_render_board_shows_numbers_and_styled_marks() -> None:
    """
    Target: tictactoe_demo.tui_specs.render_board

    Test-authoring rules:
    - Import `Mark` directly from `tictactoe_demo.core_specs`.
    - Do not inspect `new_game.__globals__` or wrapper internals.
    - Render with `Console(record=True)`.

    Verify:
    - empty cells render as move hints 1 through 9
    - occupied cells render X and O instead of numbers
    - the return value is a Rich Table
    - X and O are rendered with visible styling
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_render_screen_includes_title_status_help_and_optional_message() -> None:
    """
    Target: tictactoe_demo.tui_specs.render_screen

    Test-authoring rules:
    - Import `Mark` directly from `tictactoe_demo.core_specs` if needed.
    - Use the public `GameState.next_mark` field name.
    - Render with `Console(record=True)`.

    Verify:
    - the screen includes a title or heading for Rich Tic-Tac-Toe
    - it explains that moves are entered as numbers 1..9 and q quits
    - it includes the current board renderable
    - it includes status for whose turn it is or the final result
    - when message='Cell 5 is already occupied', that text appears clearly
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_play_cli_handles_invalid_input_then_quit() -> None:
    """
    Target: tictactoe_demo.tui_specs.play_cli

    Test-authoring rules:
    - Import `Mark` directly from `tictactoe_demo.core_specs`.
    - Use a Rich Console with `record=True` and an explicit scripted input
      function.
    - Do not inspect wrapper internals.

    Script:
    - choose X
    - enter invalid text such as "hello"
    - enter q

    Verify:
    - the app re-prompts instead of crashing
    - the transcript contains a helpful invalid-input message
    - the function returns "quit"
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_play_cli_can_run_a_full_scripted_session() -> None:
    """
    Target: tictactoe_demo.tui_specs.play_cli

    Test-authoring rules:
    - Import `Mark` directly from `tictactoe_demo.core_specs`.
    - Use a Rich Console with `record=True`.
    - Do not recover types through `__globals__`.

    Use a deterministic scripted input function that:
    - chooses a side
    - plays a complete legal game against the optimal AI
    - declines replay at the end

    Verify:
    - the session terminates without uncaught exceptions
    - the final return value is one of "human", "ai", or "draw"
    - the transcript includes the final board and a result banner
    - the AI never makes an illegal move
    - the AI never asks for human input on the AI turn
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_main_returns_zero_for_quit_or_completed_game() -> None:
    """
    Target: tictactoe_demo.tui_specs.main

    Test-authoring rules:
    - Use public functions only.
    - Avoid wrapper internals.

    Verify:
    - main() constructs a Console and delegates to play_cli
    - a quit outcome returns 0
    - a completed game outcome also returns 0
    """
    raise AssertionError("spec stub (generated at test time)")
