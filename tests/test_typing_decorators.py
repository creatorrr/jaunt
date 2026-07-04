"""Type-checker contract for Jaunt's decorators.

This module is validated by ``uv run ty check`` (it lives under ``tests/`` which
ty checks). The decorated symbols live in a ``TYPE_CHECKING`` block so the type
assertions are exercised by ty without registering specs at import time.

Regression target (FEEDBACK finding 3): ``@jaunt.magic`` in every form must be
signature-preserving. Before the fix, ``@jaunt.magic()`` erased the decorated
symbol to ``Any``; the ``assert_type`` calls below caught that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Union

    from typing_extensions import assert_type

    import jaunt

    @jaunt.magic()
    def f_called(x: int) -> str: ...

    @jaunt.magic
    def f_bare(x: int) -> str: ...

    @jaunt.magic(deps=[])
    def f_kwargs(x: int) -> str: ...

    @jaunt.sig
    def _standalone_sig_unused(x: int) -> str: ...  # typing shape only; misuse checked at runtime

    @jaunt.magic()
    class Timer:
        def __init__(self, name: str) -> None: ...
        def elapsed(self) -> float: ...

    @jaunt.magic(deps=[])
    class MockTimer:
        def __init__(self) -> None: ...

    def _use_functions() -> None:
        # Every decorator form must preserve the wrapped signature/return type.
        assert_type(f_called(1), str)
        assert_type(f_bare(1), str)
        assert_type(f_kwargs(1), str)

    def _use_class_constructor() -> float:
        # The class constructor signature is preserved (finding 3: consumers saw
        # "Expected 0 positional arguments" for ``Timer(name)``).
        return Timer("clock").elapsed()

    def _use_in_type_position(a: Union[Timer, "MockTimer"]) -> Timer | MockTimer:
        # Decorated class names must remain usable in type positions.
        return a


def test_typing_decorators_module_is_type_checked() -> None:
    """Runtime marker: the type-level contract above is enforced by ``ty check``."""
    assert True
