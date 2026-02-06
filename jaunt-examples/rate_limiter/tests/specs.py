"""
Rate Limiter — Test Specs
"""

from __future__ import annotations

import jaunt


@jaunt.test()
def test_allows_up_to_limit() -> None:
    """
    With max_requests=3, the first 3 calls to allow("ip-1")
    should return True. The 4th should return False.

    Use a fake clock callable fixed at t=1000.0.
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_window_expiry_frees_capacity() -> None:
    """
    With max_requests=2 and window_seconds=10:

    - At t=0: allow("k") twice -> both True
    - At t=5: allow("k") -> False (still in window)
    - At t=11: allow("k") -> True (first two requests expired)

    Use a fake clock callable whose return value you can advance.
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_remaining_count() -> None:
    """
    With max_requests=5:
    - Before any requests: remaining("k") == 5
    - After 3 allowed requests: remaining("k") == 2
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_independent_keys() -> None:
    """
    Requests under key "a" should not affect the limit for key "b".

    With max_requests=1:
    - allow("a") -> True
    - allow("b") -> True
    - allow("a") -> False
    - allow("b") -> False
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_invalid_constructor_args() -> None:
    """
    - max_requests=0 should raise ValueError
    - window_seconds=0 should raise ValueError
    - window_seconds=-1 should raise ValueError
    """
    raise AssertionError("spec stub (generated at test time)")
