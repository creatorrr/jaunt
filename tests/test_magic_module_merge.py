from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

from jaunt.registry import (
    ModuleMagicDefaults,
    clear_registries,
    get_magic_registry,
    register_module_magic,
)
from jaunt.runtime import magic


def decorated_spec(x: int) -> int: ...  # type: ignore[empty-body]


def override_spec() -> None: ...


def plain_spec() -> None: ...


def _import_module_from_source(tmp_path: Path, module_name: str, source: str):
    path = tmp_path / f"{module_name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_decorated_symbol_inherits_module_defaults_per_key() -> None:
    clear_registries()
    register_module_magic(
        ModuleMagicDefaults(
            module=__name__,
            source_file=__file__,
            decorator_kwargs={"prompt": "module prompt", "infer_deps": False},
        )
    )
    magic(deps=["json"])(decorated_spec)
    entry = next(e for e in get_magic_registry().values() if e.qualname == "decorated_spec")
    assert entry.decorator_kwargs["prompt"] == "module prompt"
    assert entry.decorator_kwargs["infer_deps"] is False
    assert entry.decorator_kwargs["deps"] == ["json"]
    assert entry.origin == "decorator"


def test_per_symbol_kwarg_wins_over_module_default() -> None:
    clear_registries()
    register_module_magic(
        ModuleMagicDefaults(
            module=__name__, source_file=__file__, decorator_kwargs={"prompt": "module"}
        )
    )
    magic(prompt="mine")(override_spec)
    entry = next(e for e in get_magic_registry().values() if e.qualname == "override_spec")
    assert entry.decorator_kwargs["prompt"] == "mine"


def test_no_module_entry_means_no_merge() -> None:
    clear_registries()
    magic()(plain_spec)
    entry = next(e for e in get_magic_registry().values() if e.qualname == "plain_spec")
    assert entry.decorator_kwargs == {}


def test_whole_class_with_sig_method_in_governed_module_does_not_trip_absorption(
    tmp_path: Path, monkeypatch
) -> None:
    # Absorption path eagerly imports the generated module for classes; force not-built.
    monkeypatch.setattr(
        "jaunt.runtime.importlib.import_module",
        lambda name: (_ for _ in ()).throw(ModuleNotFoundError(name)),
    )
    clear_registries()
    module_name = "tmp_governed_sealed"
    register_module_magic(
        ModuleMagicDefaults(
            module=module_name,
            source_file=f"{module_name}.py",
            decorator_kwargs={"prompt": "module"},
        )
    )
    src = '''
    import jaunt

    @jaunt.magic()
    class Sealed:
        """Whole-class spec."""

        @jaunt.sig
        def method(self, x: int) -> int:
            ...
    '''
    try:
        _import_module_from_source(tmp_path, module_name, src)
        class_entry = next(e for e in get_magic_registry().values() if e.qualname == "Sealed")
        assert class_entry.sealed_members == ("method",)
        assert class_entry.decorator_kwargs["prompt"] == "module"
    finally:
        sys.modules.pop(module_name, None)
