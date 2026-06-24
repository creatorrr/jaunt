from __future__ import annotations

import jaunt


def test_preserve_bare_is_identity() -> None:
    def f() -> int:
        return 1

    assert jaunt.preserve(f) is f


def test_preserve_called_is_identity() -> None:
    def f() -> int:
        return 1

    assert jaunt.preserve()(f) is f


def test_preserve_exported() -> None:
    assert "preserve" in jaunt.__all__
