"""Error formatting and actionable hints for Jaunt CLI output.

Keep this module small and dependency-light: it is imported by the CLI layer
and only depends on the error hierarchy.
"""

from __future__ import annotations

from jaunt.errors import (
    JauntConfigError,
    JauntDependencyCycleError,
    JauntDiscoveryError,
)


def format_build_failures(failed: dict[str, list[str]]) -> str:
    """Format build failures into a human-readable stderr summary."""
    if not failed:
        return ""
    lines = [f"Build failed for {len(failed)} module(s):\n"]
    for mod in sorted(failed):
        lines.append(f"  {mod}:")
        for err in failed[mod]:
            lines.append(f"    - {err}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_test_generation_failures(failed: dict[str, list[str]]) -> str:
    """Format test generation failures into a human-readable stderr summary."""
    if not failed:
        return ""
    lines = [f"Test generation failed for {len(failed)} module(s):\n"]
    for mod in sorted(failed):
        lines.append(f"  {mod}:")
        for err in failed[mod]:
            lines.append(f"    - {err}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_hint(exc: BaseException) -> str | None:
    """Return an actionable hint for a known error, or None."""
    msg = str(exc)

    if isinstance(exc, JauntConfigError):
        if "jaunt.toml" in msg and "find" in msg.lower():
            return "run `jaunt init` to create a new project"
        if "Missing API key" in msg:
            return "create a .env file in your project root with the key"
        return None

    if isinstance(exc, JauntDiscoveryError):
        return "check that paths.source_roots in jaunt.toml includes the correct directories"

    if isinstance(exc, JauntDependencyCycleError):
        return (
            "break the cycle by removing a `deps=` reference "
            "or setting `infer_deps=False` on one spec"
        )

    if isinstance(exc, KeyError) and exc.args:
        name = exc.args[0]
        if isinstance(name, str) and name:
            return "create a .env file in your project root or export the variable in your shell"

    return None


def format_error_with_hint(exc: BaseException) -> str:
    """Format error message + optional hint for stderr output."""
    # Special KeyError formatting (env var names).
    if isinstance(exc, KeyError) and exc.args:
        name = exc.args[0]
        if isinstance(name, str) and name:
            msg = f"missing environment variable {name}"
        else:
            msg = (str(exc) or repr(exc)).strip()
    else:
        msg = (str(exc) or repr(exc)).strip()

    result = f"error: {msg}"
    hint = format_hint(exc)
    if hint:
        result += f"\nhint: {hint}"
    return result
