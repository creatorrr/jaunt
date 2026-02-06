from __future__ import annotations

import jaunt


def test_magic_and_test_are_callable() -> None:
    assert callable(jaunt.magic)
    assert callable(jaunt.test)


def test_exceptions_are_exported() -> None:
    from jaunt import (  # noqa: PLC0415
        JauntConfigError,
        JauntDependencyCycleError,
        JauntDiscoveryError,
        JauntError,
        JauntGenerationError,
        JauntNotBuiltError,
    )

    for exc in (
        JauntError,
        JauntConfigError,
        JauntDiscoveryError,
        JauntNotBuiltError,
        JauntGenerationError,
        JauntDependencyCycleError,
    ):
        assert issubclass(exc, Exception)
