"""Contract-mode properties in practice: conservation, round-trips, bounds.

Every ``Properties:`` bullet here is Tier 1 — ``given <bindings> :: <invariant>``
parses deterministically, so ``jaunt reconcile`` needs no model and no API key
for this example. Prose bullets (Tier 2) are also supported: reconcile sends
them through the model, which transcribes the stated invariant into the same
``given … :: …`` form for review.
"""

import jaunt


@jaunt.contract
def allocate(total: int, parts: int) -> list[int]:
    """Split an amount in cents into near-equal integer shares, larger first.

    The classic "no lost pennies" function: the shares must always sum back to
    the original total, for negative totals too. Pinned examples cannot see a
    float-truncation bug (``int(total / parts)`` instead of ``divmod``) because
    it only misallocates for negative totals — the conservation property
    falsifies it at ``t=-1, n=2``.

    Examples:
    - allocate(100, 3) == [34, 33, 33]
    - allocate(7, 7) == [1, 1, 1, 1, 1, 1, 1]

    Properties:
    - given t: int, n: st.integers(min_value=1, max_value=50) :: sum(allocate(t, n)) == t
    - given t: int, n: st.integers(min_value=1, max_value=50) :: max(allocate(t, n)) - min(allocate(t, n)) <= 1
    """
    q, r = divmod(total, parts)
    return [q + 1] * r + [q] * (parts - r)


@jaunt.contract
def chunked(xs: list[int], size: int) -> list[list[int]]:
    """Split a list into consecutive chunks of at most ``size`` items.

    The flatten round-trip pins the whole behavior in one line: no item lost,
    none duplicated, order preserved.

    Examples:
    - chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]

    Properties:
    - given xs: st.lists(st.integers()), n: st.integers(min_value=1, max_value=10) :: sum(chunked(xs, n), []) == xs
    - given xs: st.lists(st.integers()), n: st.integers(min_value=1, max_value=10) :: all(len(c) <= n for c in chunked(xs, n))
    """
    return [xs[i : i + size] for i in range(0, len(xs), size)]


@jaunt.contract
def rle_encode(xs: list[int]) -> list[tuple[int, int]]:
    """Run-length encode a list into (value, count) pairs.

    A codec's strongest contract is the round-trip through its partner —
    the invariant may reference other module functions (``rle_decode`` here),
    and the battery imports them automatically.

    Examples:
    - rle_encode([1, 1, 2, 2, 2, 1]) == [(1, 2), (2, 3), (1, 1)]

    Properties:
    - given xs: st.lists(st.integers()) :: rle_decode(rle_encode(xs)) == xs
    - given xs: st.lists(st.integers()) :: all(count > 0 for value, count in rle_encode(xs))
    - given xs: st.lists(st.integers()) :: all(a[0] != b[0] for a, b in zip(rle_encode(xs), rle_encode(xs)[1:]))
    """
    out: list[tuple[int, int]] = []
    for x in xs:
        if out and out[-1][0] == x:
            out[-1] = (x, out[-1][1] + 1)
        else:
            out.append((x, 1))
    return out


@jaunt.contract
def rle_decode(pairs: list[tuple[int, int]]) -> list[int]:
    """Expand (value, count) pairs back into the original list.

    Examples:
    - rle_decode([(1, 2), (2, 3)]) == [1, 1, 2, 2, 2]

    Properties:
    - given xs: st.lists(st.integers()) :: rle_decode(rle_encode(xs)) == xs
    """
    return [value for value, count in pairs for _ in range(count)]
