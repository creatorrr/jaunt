import pytest

from jaunt.errors import (
    JauntConfigError,
    JauntDependencyCycleError,
    JauntDiscoveryError,
    JauntError,
    JauntGenerationError,
    JauntNotBuiltError,
)


def test_all_errors_are_subclasses_of_jaunt_error() -> None:
    assert issubclass(JauntConfigError, JauntError)
    assert issubclass(JauntDiscoveryError, JauntError)
    assert issubclass(JauntNotBuiltError, JauntError)
    assert issubclass(JauntGenerationError, JauntError)
    assert issubclass(JauntDependencyCycleError, JauntError)


def test_error_message_is_preserved() -> None:
    msg = "boom"
    err = JauntConfigError(msg)
    assert str(err) == msg


def test_can_catch_any_jaunt_error() -> None:
    def raise_one() -> None:
        raise JauntGenerationError("nope")

    with pytest.raises(JauntError):
        raise_one()
