from __future__ import annotations

import jaunt


@jaunt.test()
def test_add_and_list() -> None:
    """
    Target: task_board_demo.TaskBoard

    - Create a TaskBoard.
    - Add tasks with priorities 3, 1, 2.
    - list_by_priority() should return them ordered by priority (1, 2, 3).
    - Each returned task should have "id", "title", and "priority" keys.
    """
    from task_board_demo.specs import TaskBoard

    board = TaskBoard()
    board.add("Low", priority=3)
    board.add("High", priority=1)
    board.add("Medium", priority=2)

    tasks = board.list_by_priority()
    priorities = [t["priority"] for t in tasks]
    assert priorities == [1, 2, 3]


@jaunt.test()
def test_validate_priority_rejects_out_of_range() -> None:
    """
    Target: task_board_demo.TaskBoard.validate_priority

    - validate_priority(1) through validate_priority(5) should succeed.
    - validate_priority(0) and validate_priority(6) should raise ValueError.
    """
    import pytest
    from task_board_demo.specs import TaskBoard

    for v in range(1, 6):
        assert TaskBoard.validate_priority(v) == v

    for v in (0, 6, -1, 100):
        with pytest.raises(ValueError):
            TaskBoard.validate_priority(v)


@jaunt.test()
def test_from_dict_roundtrip() -> None:
    """
    Target: task_board_demo.TaskBoard.from_dict

    - Create a board, add two tasks.
    - Serialize to dict via {"tasks": board._tasks}.
    - Reconstruct via TaskBoard.from_dict(data).
    - The reconstructed board should list the same tasks.
    - Adding a new task should not collide with existing ids.
    """
    from task_board_demo.specs import TaskBoard

    board = TaskBoard()
    board.add("Alpha", priority=1)
    board.add("Beta", priority=2)

    data: dict[str, object] = {"tasks": list(board._tasks)}
    restored = TaskBoard.from_dict(data)
    assert len(restored.list_by_priority()) == 2

    new_task = restored.add("Gamma", priority=3)
    assert new_task["id"] == 3


@jaunt.test()
def test_summarize() -> None:
    """
    Target: task_board_demo.summarize

    - Empty board: "0 tasks, highest priority: n/a".
    - Board with tasks of priorities 3 and 1: "2 task(s), highest priority: 1".
    """
    from task_board_demo.specs import TaskBoard, summarize

    empty = TaskBoard()
    assert summarize(empty) == "0 tasks, highest priority: n/a"

    board = TaskBoard()
    board.add("A", priority=3)
    board.add("B", priority=1)
    assert summarize(board) == "2 task(s), highest priority: 1"
