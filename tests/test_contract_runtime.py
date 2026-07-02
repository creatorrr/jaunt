"""@jaunt.contract runtime gate: async + whole-class admission."""

from __future__ import annotations

import pytest

import jaunt
from jaunt import registry
from jaunt.errors import JauntError


@pytest.fixture(autouse=True)
def _clean_registries():
    registry.clear_registries()
    yield
    registry.clear_registries()


def test_async_function_is_registered() -> None:
    @jaunt.contract
    async def fetch(x: int) -> int:
        """Fetch."""
        return x

    entries = registry.get_contract_registry()
    assert any(e.qualname == "fetch" for e in entries.values())


def test_class_is_registered() -> None:
    @jaunt.contract
    class Counter:
        """Counts."""

        def bump(self) -> int:
            return 1

    entries = registry.get_contract_registry()
    [entry] = [e for e in entries.values() if e.qualname == "Counter"]
    assert isinstance(entry.obj, type)


def test_method_still_rejected() -> None:
    with pytest.raises(JauntError, match="whole class"):

        class C:
            @jaunt.contract
            def m(self) -> None: ...


def test_staticmethod_still_rejected() -> None:
    with pytest.raises(JauntError):

        class C:
            @jaunt.contract
            @staticmethod
            def s() -> None: ...
