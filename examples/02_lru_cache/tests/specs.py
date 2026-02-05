from __future__ import annotations

import jaunt


@jaunt.test()
def test_lru_cache_eviction_order() -> None:
    """
    Target: cache_demo.LRUCache

    With capacity=2:
    - set("a", 1), set("b", 2), set("c", 3) should evict "a".
    - get("a") is None
    - get("b") == 2
    - get("c") == 3
    - size() == 2
    """
    from cache_demo import LRUCache

    cache = LRUCache(2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)

    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    assert cache.size() == 2


@jaunt.test()
def test_get_updates_recency() -> None:
    """
    Target: cache_demo.LRUCache

    With capacity=2:
    - set("a", 1), set("b", 2)
    - get("a") (hit) makes "a" most-recent
    - set("c", 3) should evict "b" (not "a")
    """
    from cache_demo import LRUCache

    cache = LRUCache(2)
    cache.set("a", 1)
    cache.set("b", 2)

    assert cache.get("a") == 1
    cache.set("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


@jaunt.test()
def test_overwrite_does_not_grow_size() -> None:
    """
    Target: cache_demo.LRUCache

    Overwriting an existing key should not increase size:
    - capacity=2
    - set("a", 1), set("a", 99)
    - size() == 1
    - get("a") == 99
    """
    from cache_demo import LRUCache

    cache = LRUCache(2)
    cache.set("a", 1)
    cache.set("a", 99)

    assert cache.size() == 1
    assert cache.get("a") == 99


@jaunt.test()
def test_invalid_capacity_raises() -> None:
    """
    Target: cache_demo.LRUCache

    Constructing with capacity < 1 should raise ValueError:
    - LRUCache(0) raises ValueError
    - LRUCache(-1) raises ValueError
    """
    import pytest
    from cache_demo import LRUCache

    with pytest.raises(ValueError):
        LRUCache(0)
    with pytest.raises(ValueError):
        LRUCache(-1)
