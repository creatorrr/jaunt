from __future__ import annotations

from jaunt.module_contract import synthesize_auto_class_test_entries
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _magic_class(module: str, name: str, *, test: object) -> SpecEntry:
    kwargs = {} if test is None else {"test": test}
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{name}"),
        module=module,
        qualname=name,
        source_file=f"/src/{module.replace('.', '/')}.py",
        obj=type(name, (), {}),
        decorator_kwargs=kwargs,
        class_name=None,
    )


def test_opt_in_via_kwarg() -> None:
    specs = {e.spec_ref: e for e in [_magic_class("pkg.mod", "Cart", test=True)]}
    out = synthesize_auto_class_test_entries(
        specs, default_on=False, tests_package="tests", generated_dir="__generated__"
    )
    assert len(out) == 1
    entries = next(iter(out.values()))
    assert entries[0].kind == "test"
    assert entries[0].decorator_kwargs["public_api_only"] is True
    targets = {str(t) for t in entries[0].decorator_kwargs["targets"]}
    assert "pkg.mod:Cart" in targets


def test_default_on_applies_when_kwarg_absent() -> None:
    specs = {e.spec_ref: e for e in [_magic_class("pkg.mod", "Cart", test=None)]}
    assert synthesize_auto_class_test_entries(
        specs, default_on=False, tests_package="tests", generated_dir="__generated__"
    ) == {}
    assert synthesize_auto_class_test_entries(
        specs, default_on=True, tests_package="tests", generated_dir="__generated__"
    ) != {}


def test_kwarg_false_overrides_default_on() -> None:
    specs = {e.spec_ref: e for e in [_magic_class("pkg.mod", "Cart", test=False)]}
    assert synthesize_auto_class_test_entries(
        specs, default_on=True, tests_package="tests", generated_dir="__generated__"
    ) == {}
