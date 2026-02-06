from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest

from jaunt.errors import JauntError, JauntNotBuiltError
from jaunt.registry import clear_registries, get_magic_registry
from jaunt.runtime import magic
from jaunt.spec_ref import normalize_spec_ref


def top_level_fn(x: int) -> int:
    return x + 1


class TopLevelClass:
    def __init__(self, x: int) -> None:
        self.x = x


@pytest.fixture(autouse=True)
def _clear_registries() -> Generator[None, None, None]:
    clear_registries()
    yield
    clear_registries()


def test_registers_function_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    wrapped = magic()(top_level_fn)
    reg = get_magic_registry()
    expected_ref = normalize_spec_ref(f"{top_level_fn.__module__}:{top_level_fn.__qualname__}")
    assert expected_ref in reg
    assert reg[expected_ref].kind == "magic"
    assert callable(wrapped)


def test_registers_class_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    cls = magic()(TopLevelClass)
    reg = get_magic_registry()
    expected_ref = normalize_spec_ref(f"{TopLevelClass.__module__}:{TopLevelClass.__qualname__}")
    assert expected_ref in reg
    assert isinstance(cls, type)


def test_unbuilt_function_call_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    wrapped = magic()(top_level_fn)
    with pytest.raises(JauntNotBuiltError) as exc:
        wrapped(1)
    assert "jaunt build" in str(exc.value)


def test_unbuilt_class_instantiation_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    Placeholder = magic()(TopLevelClass)
    with pytest.raises(JauntNotBuiltError):
        Placeholder(1)


def test_wrapper_preserves_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    wrapped = magic()(top_level_fn)
    assert wrapped.__name__ == top_level_fn.__name__
    assert wrapped.__wrapped__ is top_level_fn


def test_decorator_kwargs_are_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    magic(deps="pkg.mod:Dep", prompt="hello", infer_deps=False)(top_level_fn)
    expected_ref = normalize_spec_ref(f"{top_level_fn.__module__}:{top_level_fn.__qualname__}")
    got = get_magic_registry()[expected_ref]
    assert got.decorator_kwargs == {"deps": "pkg.mod:Dep", "prompt": "hello", "infer_deps": False}


def test_built_function_forwards_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def gen_fn(x: int) -> int:
        return x + 100

    def _import(_name: str) -> Any:
        # The runtime picks a generated module name; this test doesn't care what it is.
        return SimpleNamespace(**{top_level_fn.__qualname__: gen_fn})

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    wrapped = magic()(top_level_fn)
    assert wrapped(1) == 101


def test_built_class_is_substituted(monkeypatch: pytest.MonkeyPatch) -> None:
    class Generated:
        def __init__(self, x: int) -> None:
            self.x = x

    Generated.__module__ = "some.__generated__.mod"

    def _import(_name: str) -> Any:
        return SimpleNamespace(**{TopLevelClass.__qualname__: Generated})

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    got_cls = magic()(TopLevelClass)
    assert got_cls is Generated
    assert got_cls.__module__ == TopLevelClass.__module__
    assert got_cls.__jaunt_spec_ref__.endswith(f":{TopLevelClass.__qualname__}")


def test_rejects_nested_objects() -> None:
    def inner() -> None:
        return None

    with pytest.raises(JauntError):
        magic()(inner)


def test_rejects_custom_metaclass() -> None:
    class Meta(type):
        pass

    class WithMeta(metaclass=Meta):
        pass

    with pytest.raises(JauntError):
        magic()(WithMeta)


def test_runtime_respects_generated_dir_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """When JAUNT_GENERATED_DIR is set, the runtime should use it instead of __generated__."""
    monkeypatch.setenv("JAUNT_GENERATED_DIR", "__custom_gen__")

    import_calls: list[str] = []

    def _import(name: str) -> Any:
        import_calls.append(name)
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    wrapped = magic()(top_level_fn)
    with pytest.raises(JauntNotBuiltError):
        wrapped(1)

    # The import should have tried the custom generated dir, not __generated__
    assert any("__custom_gen__" in c for c in import_calls), (
        f"Expected import to use __custom_gen__, got: {import_calls}"
    )
