from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

import jaunt
from jaunt import registry
from jaunt.errors import JauntError


def teardown_function() -> None:
    registry.clear_registries()


def _import_module_from_source(tmp_path: Path, module_name: str, source: str):
    path = tmp_path / f"{module_name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


# ---------------------------------------------------------------------------
# Top-level contract specs (real module-level functions).
# These exercise the runtime no-op identity behaviour. They do NOT rely on
# registry state (which teardown_function wipes between tests).
# ---------------------------------------------------------------------------


@jaunt.contract
def slugify(title: str) -> str:
    """Lowercase. Raises ValueError if empty."""
    if not title.strip():
        raise ValueError("empty")
    return title.strip().lower()


@jaunt.contract()
def normalize(x: str) -> str:
    """Strip."""
    return x.strip()


def test_contract_is_noop_identity_bare() -> None:
    # The decorated object is the original function and runs its own body.
    # The bare form must narrow the return type to the wrapped function (F),
    # so `slugify(...) -> str` type-checks and runs.
    assert slugify("  HI ") == "hi"
    assert slugify.__name__ == "slugify"


def test_contract_does_not_raise_not_built() -> None:
    # No __generated__ import, no JauntNotBuiltError path: the committed body runs.
    assert normalize("  hi  ") == "hi"


# ---------------------------------------------------------------------------
# Registry behaviour: use importable temp modules so registration happens at
# import time against real top-level functions, independent of teardown timing.
# ---------------------------------------------------------------------------


def test_contract_called_form_registers_kind_contract(tmp_path: Path) -> None:
    src = """
    import jaunt

    @jaunt.contract()
    def normalize(x: str) -> str:
        '''Strip.'''
        return x.strip()
    """
    _import_module_from_source(tmp_path, "tmp_contract_called", src)

    entries = list(registry.get_contract_registry().values())
    assert len(entries) == 1
    assert entries[0].kind == "contract"
    assert entries[0].qualname == "normalize"
    assert registry.get_contract_registry() is not registry.get_magic_registry()


def test_contract_bare_form_registers_top_level_qualname(tmp_path: Path) -> None:
    src = """
    import jaunt

    @jaunt.contract
    def slugify(title: str) -> str:
        '''Lowercase. Raises ValueError if empty.'''
        return title.strip().lower()
    """
    _import_module_from_source(tmp_path, "tmp_contract_bare", src)

    entries = list(registry.get_contract_registry().values())
    assert len(entries) == 1
    assert entries[0].kind == "contract"
    assert entries[0].qualname == "slugify"
    # qualname is consistent with the spec_ref tail.
    assert entries[0].spec_ref.endswith(":slugify")


# ---------------------------------------------------------------------------
# Runtime gate tests: nested/closure and methods still reject; classes/async admit.
# ---------------------------------------------------------------------------


def test_contract_rejects_nested_closure(tmp_path: Path) -> None:
    src = """
    import jaunt

    def outer():
        @jaunt.contract
        def inner() -> int:
            '''Return 1.'''
            return 1
        return inner

    outer()
    """
    with pytest.raises(JauntError):
        _import_module_from_source(tmp_path, "tmp_contract_closure", src)


def test_contract_registers_class() -> None:
    @jaunt.contract
    class Widget:
        """A class."""

    entries = list(registry.get_contract_registry().values())
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "contract"
    assert entry.qualname == "Widget"
    assert isinstance(entry.obj, type)


def test_contract_rejects_method(tmp_path: Path) -> None:
    src = """
    import jaunt

    class Widget:
        @jaunt.contract
        def render(self) -> str:
            '''Render.'''
            return 'x'
    """
    with pytest.raises(JauntError):
        _import_module_from_source(tmp_path, "tmp_contract_method", src)


def test_contract_registers_async() -> None:
    @jaunt.contract
    async def fetch() -> int:
        """Async return."""
        return 1

    entries = list(registry.get_contract_registry().values())
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "contract"
    assert entry.qualname == "fetch"
    assert entry.obj is fetch
