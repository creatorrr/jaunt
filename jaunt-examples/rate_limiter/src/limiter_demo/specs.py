"""
Rate Limiter — Jaunt Example

A sliding-window rate limiter suitable for API endpoints.
"""

from __future__ import annotations

from collections.abc import Callable

import jaunt


@jaunt.magic()
class SlidingWindowLimiter:
    """
    A sliding-window rate limiter keyed by arbitrary string identifiers.

    Behavior:
    - Track request timestamps per key within a rolling window.
    - `allow(key)` returns True and records the request if under the limit,
      or returns False without recording if the limit is reached.
    - `remaining(key)` returns how many requests are left in the current window
      at the current clock time (never negative).
    - `reset_at(key)` returns the unix timestamp when the oldest in-window
      request expires (oldest_timestamp + window_seconds), or None if there are
      no in-window requests.

    Constructor:
    - max_requests: int — max allowed per window (must be >= 1).
    - window_seconds: float — window duration (must be > 0).
    - clock: Callable[[], float] — injectable time source (default: use time.time).

    Cleanup:
    - Expired timestamps should be pruned on every call to allow().
      (Implementations may also prune on remaining()/reset_at() for accuracy.)
    - Do NOT let memory grow unbounded across keys — prune stale keys
      that have no timestamps left in the window.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        raise RuntimeError("spec stub (generated at build time)")

    def allow(self, key: str) -> bool:
        """
        Return True if the request is allowed for `key` and record its timestamp.

        - Always prune expired timestamps for `key` before checking the limit.
        - If after pruning the count is < max_requests: record now and return True.
        - Else: return False without recording.
        - If pruning leaves `key` with an empty window, remove the key entirely.
        """
        raise RuntimeError("spec stub (generated at build time)")

    def remaining(self, key: str) -> int:
        """Return max_requests - in_window_count(key) at the current clock time."""
        raise RuntimeError("spec stub (generated at build time)")

    def reset_at(self, key: str) -> float | None:
        """Return oldest_in_window_timestamp(key) + window_seconds, or None."""
        raise RuntimeError("spec stub (generated at build time)")
