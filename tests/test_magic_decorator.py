from __future__ import annotations

import abc
import importlib.util
import inspect
import sys
import textwrap
from collections.abc import Generator
from pathlib import Path
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


class HostClass:
    """A top-level class whose raw methods are used for method decorator tests."""

    def regular_method(self, uid: int) -> dict:  # type: ignore[empty-body]
        """Get a user by ID."""
        ...

    async def async_method(self, uid: int) -> dict:  # type: ignore[empty-body]
        """Async method stub."""
        ...

    @classmethod
    def cls_method(cls, config: dict) -> HostClass:  # type: ignore[empty-body]
        """Create from config."""
        ...

    @staticmethod
    def static_method(value: int) -> bool:  # type: ignore[empty-body]
        """Validate a value."""
        ...


class AutoTestClass:
    """spec for the magic(test=...) pass-through test."""


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


def test_bare_and_called_forms_register_function_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    def gen_fn(x: int) -> int:
        return x + 100

    def _import(_name: str) -> Any:
        return SimpleNamespace(**{top_level_fn.__qualname__: gen_fn})

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    wrapped_bare = magic(top_level_fn)
    expected_ref = normalize_spec_ref(f"{top_level_fn.__module__}:{top_level_fn.__qualname__}")
    reg = get_magic_registry()
    assert expected_ref in reg
    assert reg[expected_ref].kind == "magic"
    assert callable(wrapped_bare)
    assert wrapped_bare(1) == 101

    clear_registries()

    wrapped_called = magic()(top_level_fn)
    reg = get_magic_registry()
    assert expected_ref in reg
    assert reg[expected_ref].kind == "magic"
    assert callable(wrapped_called)
    assert wrapped_called(1) == 101


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


def _import_module_from_source(tmp_path: Path, module_name: str, source: str):
    path = tmp_path / f"{module_name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_nested_decorators_capture_app_and_post_metadata(tmp_path: Path) -> None:
    module_name = "tmp_nested_magic_metadata"
    src = """
    import functools
    import jaunt

    class App:
        def post(self, fn):
            @functools.wraps(fn)
            def wrapped(*args, **kwargs):
                return fn(*args, **kwargs)
            return wrapped

    app = App()

    def logger(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            return fn(*args, **kwargs)
        return wrapped

    @logger
    @jaunt.magic()
    @app.post
    def handler(req: str) -> str:
        return req
    """
    try:
        _import_module_from_source(tmp_path, module_name, src)
        ref = normalize_spec_ref(f"{module_name}:handler")
        entry = get_magic_registry()[ref]
        seen = {r.symbol_path for r in entry.decorator_api_records}
        assert "app" in seen
        assert "app.post" in seen
        assert entry.effective_signature is not None
    finally:
        sys.modules.pop(module_name, None)


def test_magic_source_fallback_for_closure_wrapped_decorators(tmp_path: Path) -> None:
    module_name = "tmp_nested_magic_source_fallback"
    src = """
    import jaunt

    class App:
        def post(self, fn):
            def wrapped(*args, **kwargs):
                return fn(*args, **kwargs)
            return wrapped

    app = App()

    @jaunt.magic()
    @app.post
    def handler(req: str) -> str:
        return req
    """
    try:
        _import_module_from_source(tmp_path, module_name, src)
        ref = normalize_spec_ref(f"{module_name}:handler")
        entry = get_magic_registry()[ref]
        assert entry.qualname == "handler"
        assert entry.effective_signature == "(req: str) -> str"
        assert entry.effective_signature_source == "original"
        assert any("weak decorator type metadata" in w for w in entry.decorator_warnings)
    finally:
        sys.modules.pop(module_name, None)


# ---------------------------------------------------------------------------
# Method decorator tests
# ---------------------------------------------------------------------------

# Grab raw functions from the class dict (before descriptor wrapping).
_raw_regular = HostClass.__dict__["regular_method"]
_raw_async = HostClass.__dict__["async_method"]
_raw_cls = HostClass.__dict__["cls_method"].__func__  # unwrap classmethod
_raw_static = HostClass.__dict__["static_method"].__func__  # unwrap staticmethod


class TestMethodRegistration:
    """Tests for @magic() on class methods — registration and metadata."""

    def test_regular_method_registers_with_class_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(_raw_regular)
        reg = get_magic_registry()
        expected_ref = normalize_spec_ref(f"{_raw_regular.__module__}:{_raw_regular.__qualname__}")
        assert expected_ref in reg
        entry = reg[expected_ref]
        assert entry.class_name == "HostClass"
        assert entry.qualname == "HostClass.regular_method"
        assert callable(wrapped)

    def test_top_level_function_has_no_class_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        magic()(top_level_fn)
        reg = get_magic_registry()
        expected_ref = normalize_spec_ref(f"{top_level_fn.__module__}:{top_level_fn.__qualname__}")
        assert reg[expected_ref].class_name is None

    def test_classmethod_function_registers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        magic()(_raw_cls)
        reg = get_magic_registry()
        expected_ref = normalize_spec_ref(f"{_raw_cls.__module__}:{_raw_cls.__qualname__}")
        assert expected_ref in reg
        assert reg[expected_ref].class_name == "HostClass"

    def test_staticmethod_function_registers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        magic()(_raw_static)
        reg = get_magic_registry()
        expected_ref = normalize_spec_ref(f"{_raw_static.__module__}:{_raw_static.__qualname__}")
        assert expected_ref in reg
        assert reg[expected_ref].class_name == "HostClass"

    def test_method_preserves_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(_raw_regular)
        assert wrapped.__name__ == "regular_method"
        assert wrapped.__wrapped__ is _raw_regular


class TestMethodWrapper:
    """Tests for @magic() on class methods — runtime wrapper behavior."""

    def test_unbuilt_method_raises_not_built_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(_raw_regular)
        instance = object.__new__(HostClass)
        with pytest.raises(JauntNotBuiltError, match="jaunt build"):
            wrapped(instance, 1)

    def test_built_method_delegates_to_generated_class(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class GenClass:
            def regular_method(self, uid: int) -> dict:
                return {"id": uid, "generated": True}

        def _import(_name: str) -> Any:
            return SimpleNamespace(HostClass=GenClass)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(_raw_regular)
        instance = object.__new__(HostClass)
        result = wrapped(instance, 42)
        assert result == {"id": 42, "generated": True}

    def test_async_method_registers_and_is_coroutine_function(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(_raw_async)
        assert inspect.iscoroutinefunction(wrapped)

    def test_async_method_delegates_when_built(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        class GenClass:
            async def async_method(self, uid: int) -> dict:
                return {"id": uid, "async_generated": True}

        def _import(_name: str) -> Any:
            return SimpleNamespace(HostClass=GenClass)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(_raw_async)
        instance = object.__new__(HostClass)
        result = asyncio.run(wrapped(instance, 7))
        assert result == {"id": 7, "async_generated": True}

    def test_classmethod_delegates_correctly_when_generated_uses_classmethod(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Generated class with @classmethod must not double-pass cls."""

        class GenClass:
            @classmethod
            def cls_method(cls, config: dict) -> str:
                return f"built-{config['key']}"

        def _import(_name: str) -> Any:
            return SimpleNamespace(HostClass=GenClass)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(_raw_cls)
        # Simulate classmethod descriptor: Python passes cls as first arg
        result = wrapped(HostClass, {"key": "val"})
        assert result == "built-val"

    def test_staticmethod_delegates_correctly_when_generated_uses_staticmethod(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Generated class with @staticmethod must not inject extra self/cls."""

        class GenClass:
            @staticmethod
            def static_method(value: int) -> bool:
                return value > 0

        def _import(_name: str) -> Any:
            return SimpleNamespace(HostClass=GenClass)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        wrapped = magic()(_raw_static)
        assert wrapped(42) is True
        assert wrapped(-1) is False


class TestMethodEdgeCases:
    """Edge cases and error conditions for method decoration."""

    def test_still_rejects_closures(self) -> None:
        def outer():
            def inner():
                return None

            return inner

        fn = outer()
        with pytest.raises(JauntError):
            magic()(fn)

    def test_rejects_classmethod_descriptor(self) -> None:
        """Passing a classmethod descriptor (wrong order) should raise."""
        raw_descriptor = HostClass.__dict__["cls_method"]
        assert isinstance(raw_descriptor, classmethod)
        with pytest.raises(JauntError, match="classmethod|staticmethod|decorator order"):
            magic()(raw_descriptor)

    def test_rejects_staticmethod_descriptor(self) -> None:
        """Passing a staticmethod descriptor (wrong order) should raise."""
        raw_descriptor = HostClass.__dict__["static_method"]
        assert isinstance(raw_descriptor, staticmethod)
        with pytest.raises(JauntError, match="classmethod|staticmethod|decorator order"):
            magic()(raw_descriptor)


class TestAbstractMethodSupport:
    """Tests for @abstractmethod @magic() stacking."""

    def test_abstract_wrapper_stays_abstract_when_unbuilt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _import(_name: str) -> Any:
            raise ModuleNotFoundError(_name)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        # Simulate: @abstractmethod @magic() def process(self): ...
        # Step 1: magic() wraps the raw function
        def process(self) -> None:
            """Abstract method stub."""
            ...

        process.__qualname__ = "AbstractHost.process"
        process.__module__ = __name__

        wrapper = magic()(process)
        # Step 2: abstractmethod marks it
        abstract_wrapper = abc.abstractmethod(wrapper)
        assert getattr(abstract_wrapper, "__isabstractmethod__", False) is True

    def test_abstract_flag_cleared_after_successful_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class GenClass:
            def process(self) -> None:
                pass

        def _import(_name: str) -> Any:
            return SimpleNamespace(AbstractHost=GenClass)

        monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

        def process(self) -> None:
            """Abstract method stub."""
            ...

        process.__qualname__ = "AbstractHost.process"
        process.__module__ = __name__

        wrapper = magic()(process)
        # Mark as abstract
        wrapper.__isabstractmethod__ = True

        # Call it — should succeed and clear the flag
        wrapper(
            object(),
        )
        assert getattr(wrapper, "__isabstractmethod__", False) is False


def test_magic_test_kwarg_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    magic(test=True)(AutoTestClass)
    ref = normalize_spec_ref(f"{AutoTestClass.__module__}:{AutoTestClass.__qualname__}")
    entry = get_magic_registry()[ref]
    assert entry.decorator_kwargs.get("test") is True


def test_whole_class_records_project_base_dep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    clear_registries()
    module_name = "tmp_whole_class_base_dep"
    src = '''
    import jaunt

    @jaunt.magic()
    class Base:
        """base spec"""

    @jaunt.magic()
    class Child(Base):
        """child spec"""
    '''

    try:
        _import_module_from_source(tmp_path, module_name, src)
        child_ref = normalize_spec_ref(f"{module_name}:Child")
        entry = get_magic_registry()[child_ref]
        base_strs = {str(d) for d in entry.base_deps}
        assert any(d.endswith(":Base") for d in base_strs)
        # The base ref is recorded in base_deps, NOT merged into auto_deps.
        auto_strs = {str(d) for d in entry.auto_deps}
        assert not any(d.endswith(":Base") for d in auto_strs)
    finally:
        sys.modules.pop(module_name, None)


# ---------------------------------------------------------------------------
# Whole-class absorption of inner @magic method specs (sealed tier)
# ---------------------------------------------------------------------------


def test_inner_magic_absorbed_into_whole_class_spec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    clear_registries()
    module_name = "tmp_absorb_basic"
    src = '''
    import jaunt

    @jaunt.magic()
    class Engine:
        """An engine."""

        @jaunt.magic
        def start(self, power: int) -> bool: ...

        def helper(self): ...
    '''
    try:
        _import_module_from_source(tmp_path, module_name, src)
        reg = get_magic_registry()
        refs = [str(r) for r in reg]
        # No phantom method spec — the inner @magic is absorbed into the class.
        assert refs == [f"{module_name}:Engine"]
        entry = reg[normalize_spec_ref(f"{module_name}:Engine")]
        assert entry.sealed_members == ("start",)
    finally:
        sys.modules.pop(module_name, None)


def test_inner_magic_original_function_restored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    clear_registries()
    module_name = "tmp_absorb_restore"
    src = '''
    import jaunt

    @jaunt.magic()
    class Engine:
        """An engine."""

        @jaunt.magic
        def start(self, power: int) -> bool: ...
    '''
    try:
        _import_module_from_source(tmp_path, module_name, src)
        entry = get_magic_registry()[normalize_spec_ref(f"{module_name}:Engine")]
        cls_obj = entry.obj
        assert isinstance(cls_obj, type)
        member = cls_obj.__dict__["start"]
        # The registered class member is the restored original stub, not a wrapper.
        assert not hasattr(member, "__wrapped__")
        assert member.__name__ == "start"
    finally:
        sys.modules.pop(module_name, None)


def test_inner_magic_classmethod_descriptor_reconstructed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    clear_registries()
    module_name = "tmp_absorb_classmethod"
    src = '''
    import jaunt

    @jaunt.magic()
    class Engine:
        """doc"""

        @classmethod
        @jaunt.magic
        def make(cls) -> "Engine": ...
    '''
    try:
        _import_module_from_source(tmp_path, module_name, src)
        entry = get_magic_registry()[normalize_spec_ref(f"{module_name}:Engine")]
        cls_obj = entry.obj
        assert isinstance(cls_obj, type)
        assert isinstance(cls_obj.__dict__["make"], classmethod)
        assert entry.sealed_members == ("make",)
    finally:
        sys.modules.pop(module_name, None)


def test_inner_magic_staticmethod_descriptor_reconstructed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    clear_registries()
    module_name = "tmp_absorb_staticmethod"
    src = '''
    import jaunt

    @jaunt.magic()
    class Engine:
        """doc"""

        @staticmethod
        @jaunt.magic
        def validate(value: int) -> bool: ...
    '''
    try:
        _import_module_from_source(tmp_path, module_name, src)
        entry = get_magic_registry()[normalize_spec_ref(f"{module_name}:Engine")]
        cls_obj = entry.obj
        assert isinstance(cls_obj, type)
        assert isinstance(cls_obj.__dict__["validate"], staticmethod)
        assert entry.sealed_members == ("validate",)
    finally:
        sys.modules.pop(module_name, None)


def test_inner_magic_abstractmethod_flag_carried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    clear_registries()
    module_name = "tmp_absorb_abstract"
    # A plain class (metaclass ``type``) keeps the metaclass guard happy; the
    # ``@abc.abstractmethod`` marker only sets ``__isabstractmethod__`` on the
    # function, which is what we assert is carried onto the restored original.
    src = '''
    import abc
    import jaunt

    @jaunt.magic()
    class Engine:
        """doc"""

        @abc.abstractmethod
        @jaunt.magic
        def start(self) -> None: ...
    '''
    try:
        _import_module_from_source(tmp_path, module_name, src)
        entry = get_magic_registry()[normalize_spec_ref(f"{module_name}:Engine")]
        cls_obj = entry.obj
        assert isinstance(cls_obj, type)
        member = cls_obj.__dict__["start"]
        assert getattr(member, "__isabstractmethod__", False) is True
        assert entry.sealed_members == ("start",)
    finally:
        sys.modules.pop(module_name, None)


def test_inner_magic_with_kwargs_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    clear_registries()
    module_name = "tmp_absorb_kwargs"
    src = '''
    import jaunt

    @jaunt.magic()
    class Engine:
        """doc"""

        @jaunt.magic(deps=[])
        def start(self) -> None: ...
    '''
    try:
        with pytest.raises(JauntError, match="kwargs"):
            _import_module_from_source(tmp_path, module_name, src)
    finally:
        sys.modules.pop(module_name, None)


def test_inner_magic_on_property_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    clear_registries()
    module_name = "tmp_absorb_property"
    src = '''
    import jaunt

    @jaunt.magic()
    class Engine:
        """doc"""

        @property
        @jaunt.magic
        def value(self) -> int: ...
    '''
    try:
        with pytest.raises(JauntError, match="property"):
            _import_module_from_source(tmp_path, module_name, src)
    finally:
        sys.modules.pop(module_name, None)


def test_standalone_method_magic_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _import(_name: str) -> Any:
        raise ModuleNotFoundError(_name)

    monkeypatch.setattr("jaunt.runtime.importlib.import_module", _import)

    clear_registries()
    module_name = "tmp_standalone_method"
    src = """
    import jaunt

    class Plain:
        @jaunt.magic()
        def go(self) -> int: ...
    """
    try:
        _import_module_from_source(tmp_path, module_name, src)
        # An undecorated class keeps standalone method-spec behavior (no absorption).
        refs = [str(r) for r in get_magic_registry()]
        assert refs == [f"{module_name}:Plain.go"]
    finally:
        sys.modules.pop(module_name, None)
