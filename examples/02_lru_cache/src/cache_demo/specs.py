from __future__ import annotations

import jaunt


@jaunt.magic()
class LRUCache:
    """
    A small least-recently-used (LRU) cache keyed by strings.

    Behavior:
    - Fixed positive capacity set at construction time.
    - get(key) returns the value or None. On hit, updates recency (most-recent).
    - set(key, value) inserts/updates. If inserting would exceed capacity, evict
      the least-recently-used entry.
    - size() returns the number of stored keys.
    """

    def __init__(self, capacity: int) -> None:
        """capacity must be >= 1 (otherwise raise ValueError)."""
        raise RuntimeError("spec stub (generated at build time)")

    def get(self, key: str) -> object | None:
        """Return the stored value or None; on hit, mark the key most-recent."""
        raise RuntimeError("spec stub (generated at build time)")

    def set(self, key: str, value: object) -> None:
        """Insert/update a key; evict least-recent when over capacity."""
        raise RuntimeError("spec stub (generated at build time)")

    def size(self) -> int:
        """Return the current number of entries in the cache."""
        raise RuntimeError("spec stub (generated at build time)")

