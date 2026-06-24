from __future__ import annotations

import jaunt


@jaunt.magic()
class Stack:
    """A LIFO stack of ints. push/pop/peek; pop and peek raise IndexError when empty."""

    def push(self, value: int) -> None: ...
    def pop(self) -> int: ...
    def peek(self) -> int: ...

    @jaunt.preserve
    def is_empty(self) -> bool:
        """Hand-written: kept verbatim even though it looks tiny."""
        return len(self._items) == 0  # noqa: F821 (the generated class defines _items)


@jaunt.magic()
class Inventory:
    """Docstring-only: an item->quantity store. Supports add(item, qty),
    remove(item, qty) (never below zero), and total() across all items."""
