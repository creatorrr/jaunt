from __future__ import annotations

import warnings
from collections.abc import Generator

import pytest

from jaunt.registry import (
    ModuleMagicDefaults,
    SpecEntry,
    clear_registries,
    get_magic_registry,
    get_module_magic_defaults,
    get_specs_by_module,
    get_test_registry,
    register_magic,
    register_module_magic,
    register_test,
    unregister_magic,
)
from jaunt.spec_ref import SpecRef, normalize_spec_ref


@pytest.fixture(autouse=True)
def _clear_registries() -> Generator[None, None, None]:
    clear_registries()
    yield
    clear_registries()


def _entry(
    *,
    kind: str,
    spec_ref: str,
    module: str,
    qualname: str,
    decorator_kwargs: dict[str, object] | None = None,
) -> SpecEntry:
    return SpecEntry(
        kind=kind,  # type: ignore[arg-type]
        spec_ref=normalize_spec_ref(spec_ref),
        module=module,
        qualname=qualname,
        source_file="/fake/source.py",
        obj=object(),
        decorator_kwargs=decorator_kwargs or {},
    )


def test_register_and_retrieve_magic() -> None:
    e = _entry(
        kind="magic",
        spec_ref="pkg.mod:Magic",
        module="pkg.mod",
        qualname="Magic",
        decorator_kwargs={"x": 1},
    )
    register_magic(e)
    reg = get_magic_registry()
    assert reg[e.spec_ref] is e


def test_register_and_retrieve_test() -> None:
    e = _entry(
        kind="test",
        spec_ref="pkg.mod:Test",
        module="pkg.mod",
        qualname="Test",
        decorator_kwargs={"y": 2},
    )
    register_test(e)
    reg = get_test_registry()
    assert reg[e.spec_ref] is e


def test_registries_are_separate() -> None:
    m = _entry(kind="magic", spec_ref="pkg.mod:Thing", module="pkg.mod", qualname="Thing")
    t = _entry(kind="test", spec_ref="pkg.mod:Thing", module="pkg.mod", qualname="Thing")

    register_magic(m)
    assert m.spec_ref in get_magic_registry()
    assert m.spec_ref not in get_test_registry()

    register_test(t)
    assert t.spec_ref in get_test_registry()
    assert t.spec_ref in get_magic_registry()


def test_clear_resets_both_registries() -> None:
    register_magic(_entry(kind="magic", spec_ref="a.b:One", module="a.b", qualname="One"))
    register_test(_entry(kind="test", spec_ref="a.b:Two", module="a.b", qualname="Two"))

    assert get_magic_registry()
    assert get_test_registry()

    clear_registries()
    assert get_magic_registry() == {}
    assert get_test_registry() == {}


def test_get_specs_by_module_groups_and_sorts() -> None:
    e1 = _entry(kind="magic", spec_ref="m.one:ThingA", module="m.one", qualname="Zed")
    e2 = _entry(kind="magic", spec_ref="m.one:ThingB", module="m.one", qualname="Aaa")
    e3 = _entry(kind="magic", spec_ref="m.two:Other", module="m.two", qualname="Bbb")
    e4 = _entry(kind="magic", spec_ref="m.one:ThingC", module="m.one", qualname="Aaa")

    # Unordered insertion; ordering should be stable.
    register_magic(e1)
    register_magic(e2)
    register_magic(e3)
    register_magic(e4)

    grouped = get_specs_by_module("magic")
    assert set(grouped.keys()) == {"m.one", "m.two"}
    assert grouped["m.two"] == [e3]

    # Sorted by qualname, then spec_ref as a tie-breaker.
    assert grouped["m.one"] == [e2, e4, e1]


def _magic_entry(ref: str, *, class_name: str | None = None) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=SpecRef(ref),
        module=ref.split(":", 1)[0],
        qualname=ref.split(":", 1)[1],
        source_file="x.py",
        obj=object(),
        decorator_kwargs={},
        class_name=class_name,
    )


def test_unregister_magic_removes_and_returns_entry() -> None:
    clear_registries()
    entry = _magic_entry("m:C.f", class_name="C")
    register_magic(entry)
    assert unregister_magic(SpecRef("m:C.f")) is entry
    assert SpecRef("m:C.f") not in get_magic_registry()
    assert unregister_magic(SpecRef("m:C.f")) is None


def test_spec_entry_new_fields_default_empty() -> None:
    entry = _magic_entry("m:C")
    assert entry.sealed_members == ()
    assert entry.base_deps == ()


def test_duplicate_registration_overwrites_decorator_kwargs() -> None:
    e1 = _entry(
        kind="magic",
        spec_ref="pkg.mod:Dup",
        module="pkg.mod",
        qualname="Dup",
        decorator_kwargs={"a": 1},
    )
    e2 = _entry(
        kind="magic",
        spec_ref="pkg.mod:Dup",
        module="pkg.mod",
        qualname="Dup",
        decorator_kwargs={"a": 2},
    )

    register_magic(e1)
    register_magic(e2)

    got = get_magic_registry()[e1.spec_ref]
    assert got is e2
    assert got.decorator_kwargs == {"a": 2}


def _origin_entry(origin: str) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref("m:f"),
        module="m",
        qualname="f",
        source_file="m.py",
        obj=None,
        decorator_kwargs={},
        origin=origin,  # type: ignore[arg-type]
    )


def test_module_magic_defaults_roundtrip_and_clear() -> None:
    clear_registries()
    register_module_magic(
        ModuleMagicDefaults(module="m", source_file="m.py", decorator_kwargs={"prompt": "p"})
    )
    got = get_module_magic_defaults("m")
    assert got is not None
    assert got.decorator_kwargs == {"prompt": "p"}
    assert get_module_magic_defaults("other") is None
    clear_registries()
    assert get_module_magic_defaults("m") is None


def test_register_magic_warns_on_cross_origin_overwrite() -> None:
    clear_registries()
    register_magic(_origin_entry("module"))
    with pytest.warns(UserWarning, match="module scan and a decorator"):
        register_magic(_origin_entry("decorator"))


def test_register_magic_same_origin_overwrite_is_silent() -> None:
    clear_registries()
    register_magic(_origin_entry("module"))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        register_magic(_origin_entry("module"))
