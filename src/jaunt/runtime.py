"""On what wings dare he aspire? -- runtime decorators for declaring Jaunt specs.

Decorators register specs at import/definition time. `@magic` provides a runtime
stub that forwards to built/generated implementations when available, otherwise
raising actionable errors.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import os
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any, TypeVar, cast

from jaunt.errors import JauntError, JauntNotBuiltError
from jaunt.paths import spec_module_to_generated_module
from jaunt.registry import SpecEntry, register_magic, register_test
from jaunt.spec_ref import SpecRef, spec_ref_from_object

F = TypeVar("F", bound=Callable[..., object])


def _classify_qualname(obj: object) -> str | None:
    """Classify an object by its qualname.

    Returns the owning class name for a method (one level of nesting), or
    ``None`` for a top-level definition.  Raises for closures or deeper nesting.
    """
    o = cast(Any, obj)
    qualname: str = o.__qualname__ if hasattr(o, "__qualname__") else ""
    if "<locals>" in qualname:
        raise JauntError("Jaunt specs must not be nested inside functions (closures).")
    parts = qualname.split(".")
    if len(parts) == 1:
        return None  # top-level function/class
    if len(parts) == 2:
        return parts[0]  # ClassName.method_name → class name
    raise JauntError(
        f"Jaunt specs support at most one level of nesting (class methods), got {qualname!r}."
    )


def _source_file(obj: object) -> str:
    # Avoid filesystem I/O: capture best-effort metadata only.
    try:
        path = inspect.getsourcefile(cast(Any, obj))
    except TypeError:
        path = None
    if isinstance(path, str) and path:
        return path

    code = getattr(obj, "__code__", None)
    filename = getattr(code, "co_filename", None)
    if isinstance(filename, str) and filename:
        return filename
    return "<unknown>"


def _get_generated_dir() -> str:
    """Return the generated directory name, respecting JAUNT_GENERATED_DIR env var."""
    return os.environ.get("JAUNT_GENERATED_DIR", "__generated__")


def _import_generated_module(spec_module: str) -> ModuleType:
    generated = spec_module_to_generated_module(spec_module, generated_dir=_get_generated_dir())
    return importlib.import_module(generated)


def _not_built_error(spec_ref: SpecRef) -> JauntNotBuiltError:
    return JauntNotBuiltError(
        f"Spec {spec_ref!s} has not been built yet. Run `jaunt build` and try again."
    )


def magic(
    *,
    deps: object | None = None,
    prompt: object | None = None,
    infer_deps: object | None = None,
):
    """Decorator factory for declaring magic specs."""

    def _decorate(obj: object):
        # Guard: reject classmethod/staticmethod descriptors (wrong decorator order).
        if isinstance(obj, (classmethod, staticmethod)):
            raise JauntError(
                "@magic() must be the innermost decorator (closest to `def`). "
                "Place @classmethod/@staticmethod above @magic(), e.g.:\n"
                "    @classmethod\n"
                "    @jaunt.magic()\n"
                "    def my_method(cls): ..."
            )

        class_name = _classify_qualname(obj)

        spec_ref = spec_ref_from_object(obj)
        o = cast(Any, obj)
        module = cast(str, o.__module__)
        qualname = cast(str, o.__qualname__)
        name = cast(str, o.__name__) if hasattr(o, "__name__") else qualname

        decorator_kwargs: dict[str, object] = {}
        if deps is not None:
            decorator_kwargs["deps"] = deps
        if prompt is not None:
            decorator_kwargs["prompt"] = prompt
        if infer_deps is not None:
            decorator_kwargs["infer_deps"] = infer_deps

        entry = SpecEntry(
            kind="magic",
            spec_ref=spec_ref,
            module=module,
            qualname=qualname,
            source_file=_source_file(obj),
            obj=obj,
            decorator_kwargs=decorator_kwargs,
            class_name=class_name,
        )
        register_magic(entry)

        # Method spec: wrapper delegates to generated_module.ClassName.method_name.
        if class_name is not None:
            return _make_method_wrapper(obj, module, class_name, name, spec_ref)

        if isinstance(obj, type):
            # Reject metaclass != type (MVP constraint).
            if type(obj) is not type:
                raise JauntError("Custom metaclasses are not supported for @magic classes.")

            # MVP: import-time substitution.
            try:
                mod = _import_generated_module(module)
                gen_cls = getattr(mod, name)
            except (ModuleNotFoundError, AttributeError):

                def __new__(cls, *args: Any, **kwargs: Any):  # noqa: ANN001
                    raise _not_built_error(spec_ref)

                return type(
                    name,
                    (),
                    {"__module__": module, "__qualname__": qualname, "__new__": __new__},
                )

            if not isinstance(gen_cls, type):
                raise JauntError(
                    f"Generated symbol {module!r}:{name!r} is not a class (got {type(gen_cls)!r})."
                )

            gen_cls.__jaunt_spec_ref__ = f"{module}:{qualname}"
            gen_cls.__module__ = module
            return gen_cls

        if not callable(obj):
            raise JauntError(f"@magic can only decorate callables or classes (got {type(obj)!r}).")

        fn = cast(Callable[..., object], obj)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def _async_wrapper(*args: Any, **kwargs: Any) -> object:
                try:
                    mod = _import_generated_module(module)
                    gen_fn = getattr(mod, name)
                except (ModuleNotFoundError, AttributeError):
                    raise _not_built_error(spec_ref) from None
                return await cast(Callable[..., Awaitable[object]], gen_fn)(*args, **kwargs)

            return _async_wrapper

        @functools.wraps(fn)
        def _wrapper(*args: Any, **kwargs: Any) -> object:
            try:
                mod = _import_generated_module(module)
                gen_fn = getattr(mod, name)
            except (ModuleNotFoundError, AttributeError):
                raise _not_built_error(spec_ref) from None
            return cast(Callable[..., object], gen_fn)(*args, **kwargs)

        return _wrapper

    return _decorate


def _unwrap_from_class(cls: type, name: str) -> Callable[..., object]:
    """Get the raw function from a class, bypassing the descriptor protocol.

    ``getattr(cls, name)`` invokes descriptors — ``@classmethod`` would return
    a bound method with ``cls`` already injected, double-passing it when the
    wrapper also receives ``cls`` from the original class's descriptor.  Access
    via ``__dict__`` and unwrap ``classmethod`` / ``staticmethod`` to get the
    underlying function.
    """
    raw = cls.__dict__[name]
    if isinstance(raw, (classmethod, staticmethod)):
        return cast(Callable[..., object], raw.__func__)
    return cast(Callable[..., object], raw)


def _make_method_wrapper(
    obj: object,
    module: str,
    class_name: str,
    method_name: str,
    spec_ref: SpecRef,
) -> Callable[..., object]:
    """Create a wrapper that delegates to the generated class's method."""
    fn = cast(Callable[..., object], obj)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_method_wrapper(*args: Any, **kwargs: Any) -> object:
            try:
                mod = _import_generated_module(module)
                gen_cls = getattr(mod, class_name)
                gen_fn = _unwrap_from_class(gen_cls, method_name)
            except (ModuleNotFoundError, AttributeError, KeyError):
                raise _not_built_error(spec_ref) from None
            # Clear @abstractmethod flag once the implementation is available.
            if getattr(_async_method_wrapper, "__isabstractmethod__", False):
                _async_method_wrapper.__isabstractmethod__ = False
            return await cast(Callable[..., Awaitable[object]], gen_fn)(*args, **kwargs)

        return _async_method_wrapper

    @functools.wraps(fn)
    def _method_wrapper(*args: Any, **kwargs: Any) -> object:
        try:
            mod = _import_generated_module(module)
            gen_cls = getattr(mod, class_name)
            gen_fn = _unwrap_from_class(gen_cls, method_name)
        except (ModuleNotFoundError, AttributeError, KeyError):
            raise _not_built_error(spec_ref) from None
        # Clear @abstractmethod flag once the implementation is available.
        if getattr(_method_wrapper, "__isabstractmethod__", False):
            _method_wrapper.__isabstractmethod__ = False
        return cast(Callable[..., object], gen_fn)(*args, **kwargs)

    return _method_wrapper


def test(
    *,
    deps: object | None = None,
    prompt: object | None = None,
    infer_deps: object | None = None,
):
    """Decorator factory for declaring test specs."""

    def _decorate(fn: F) -> F:
        _classify_qualname(fn)  # rejects closures/deep nesting

        spec_ref = spec_ref_from_object(fn)
        f = cast(Any, fn)
        module = cast(str, f.__module__)
        qualname = cast(str, f.__qualname__)

        decorator_kwargs: dict[str, object] = {}
        if deps is not None:
            decorator_kwargs["deps"] = deps
        if prompt is not None:
            decorator_kwargs["prompt"] = prompt
        if infer_deps is not None:
            decorator_kwargs["infer_deps"] = infer_deps

        entry = SpecEntry(
            kind="test",
            spec_ref=spec_ref,
            module=module,
            qualname=qualname,
            source_file=_source_file(fn),
            obj=fn,
            decorator_kwargs=decorator_kwargs,
        )
        register_test(entry)

        # Prevent pytest from collecting this stub spec as a test.
        f.__test__ = False
        return fn

    return _decorate
