"""Jaunt exception hierarchy.

Keep this module small and dependency-free: it is imported broadly across the
project and by tests.
"""


class JauntError(Exception):
    """Base exception for all Jaunt errors."""


class JauntConfigError(JauntError):
    """Raised for invalid user configuration."""


class JauntDiscoveryError(JauntError):
    """Raised when module discovery/import fails."""


class JauntNotBuiltError(JauntError):
    """Raised when an operation requires a built artifact that does not exist."""


class JauntGenerationError(JauntError):
    """Raised when code generation fails."""


class JauntDependencyCycleError(JauntError):
    """Raised when a dependency graph contains a cycle."""
