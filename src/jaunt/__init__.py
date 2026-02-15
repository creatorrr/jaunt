from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from jaunt.errors import (
    JauntConfigError,
    JauntDependencyCycleError,
    JauntDiscoveryError,
    JauntError,
    JauntGenerationError,
    JauntNotBuiltError,
)
from jaunt.runtime import magic, test


def _package_version() -> str:
    try:
        return version("jaunt")
    except PackageNotFoundError:
        # Running from a source checkout, or otherwise not installed.
        return "0.0.0"


__version__ = _package_version()

__all__ = [
    "__version__",
    "magic",
    "test",
    "JauntError",
    "JauntConfigError",
    "JauntDiscoveryError",
    "JauntNotBuiltError",
    "JauntGenerationError",
    "JauntDependencyCycleError",
]
