from __future__ import annotations

from collections.abc import Generator

import pytest

from jaunt.registry import clear_registries, get_magic_registry, get_test_registry
from jaunt.runtime import test as jaunt_test
from jaunt.spec_ref import normalize_spec_ref


def top_level_test_spec() -> None:
    return None


@pytest.fixture(autouse=True)
def _clear_registries() -> Generator[None, None, None]:
    clear_registries()
    yield
    clear_registries()


def test_registers_test_spec_and_sets_pytest_flag() -> None:
    fn = jaunt_test()(top_level_test_spec)
    assert fn is top_level_test_spec
    assert callable(fn)
    assert fn.__test__ is False

    expected_ref = normalize_spec_ref(f"{fn.__module__}:{fn.__qualname__}")
    reg = get_test_registry()
    assert expected_ref in reg
    assert reg[expected_ref].kind == "test"


def test_test_specs_do_not_leak_into_magic_registry() -> None:
    jaunt_test()(top_level_test_spec)
    assert get_magic_registry() == {}


def test_stores_deps_in_decorator_kwargs() -> None:
    jaunt_test(deps=["a.b:One", "a.b:Two"])(top_level_test_spec)
    expected_ref = normalize_spec_ref(
        f"{top_level_test_spec.__module__}:{top_level_test_spec.__qualname__}"
    )
    got = get_test_registry()[expected_ref]
    assert got.decorator_kwargs == {"deps": ["a.b:One", "a.b:Two"]}


def test_stores_public_api_only_in_decorator_kwargs() -> None:
    jaunt_test(public_api_only=False)(top_level_test_spec)
    expected_ref = normalize_spec_ref(
        f"{top_level_test_spec.__module__}:{top_level_test_spec.__qualname__}"
    )
    got = get_test_registry()[expected_ref]
    assert got.decorator_kwargs == {"public_api_only": False}


def test_public_api_only_requires_boolean() -> None:
    with pytest.raises(Exception, match="public_api_only must be a boolean"):
        jaunt_test(public_api_only="nope")(top_level_test_spec)  # type: ignore[arg-type]
