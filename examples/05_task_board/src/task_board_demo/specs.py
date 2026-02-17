"""
Task Board — Jaunt Example

Demonstrates per-method @magic on a service class, including
@classmethod and @staticmethod factories/helpers.
"""

from __future__ import annotations

import jaunt


class TaskBoard:
    """
    A simple in-memory task board that tracks tasks with priorities.

    Each task is a dict: {"id": int, "title": str, "priority": int}.
    IDs auto-increment starting at 1.
    """

    def __init__(self) -> None:
        self._tasks: list[dict[str, object]] = []
        self._next_id: int = 1

    @jaunt.magic()
    def add(self, title: str, priority: int) -> dict[str, object]:
        """
        Add a task and return it.

        - Validate priority via TaskBoard.validate_priority (raise ValueError
          if out of range).
        - Title must be a non-empty string after stripping whitespace
          (raise ValueError otherwise).
        - Assign the next auto-incrementing id, starting at 1.
        - Append the task to self._tasks.
        - Return {"id": int, "title": str, "priority": int}.
        """
        raise RuntimeError("spec stub (generated at build time)")

    @jaunt.magic()
    def list_by_priority(self) -> list[dict[str, object]]:
        """
        Return all tasks sorted by priority (1 = highest first), then by id.

        Returns a new list (does not mutate internal state).
        """
        raise RuntimeError("spec stub (generated at build time)")

    @classmethod
    @jaunt.magic()
    def from_dict(cls, data: dict[str, object]) -> TaskBoard:
        """
        Factory: reconstruct a TaskBoard from a serialized dict.

        Expected shape: {"tasks": [{"id": int, "title": str, "priority": int}, ...]}.

        - Raise ValueError if `data` is missing the "tasks" key.
        - Raise ValueError if any task is missing "id", "title", or "priority".
        - Set _next_id to max(id values) + 1 so new adds don't collide.
        - Return the reconstructed TaskBoard.
        """
        raise RuntimeError("spec stub (generated at build time)")

    @staticmethod
    @jaunt.magic()
    def validate_priority(value: int) -> int:
        """
        Return `value` unchanged if it is an int in [1, 5].

        Raise ValueError with a descriptive message otherwise.
        """
        raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=[TaskBoard])
def summarize(board: TaskBoard) -> str:
    """
    Return a one-line summary of a TaskBoard.

    Format: "<count> task(s), highest priority: <p>"
    - If the board has no tasks, return "0 tasks, highest priority: n/a".
    - Use board.list_by_priority() to determine the highest priority.
    """
    raise RuntimeError("spec stub (generated at build time)")
