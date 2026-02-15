"""Tests for jaunt.diagnostics — error formatting and actionable hints."""

from __future__ import annotations

import jaunt.cli
from jaunt.diagnostics import (
    format_build_failures,
    format_error_with_hint,
    format_hint,
    format_test_generation_failures,
)
from jaunt.errors import (
    JauntConfigError,
    JauntDependencyCycleError,
    JauntDiscoveryError,
    JauntError,
)

# --- format_build_failures ---


def test_format_build_failures_single_module() -> None:
    result = format_build_failures({"pkg.mod1": ["Missing top-level definition: foo"]})
    assert "Build failed for 1 module(s):" in result
    assert "pkg.mod1" in result
    assert "- Missing top-level definition: foo" in result


def test_format_build_failures_multiple_modules() -> None:
    failed = {
        "pkg.mod2": ["Dependency failed: pkg.mod1"],
        "pkg.mod1": [
            "SyntaxError: invalid syntax (line 42:10)",
            "Missing top-level definition: foo",
        ],
    }
    result = format_build_failures(failed)
    assert "Build failed for 2 module(s):" in result
    assert "pkg.mod1" in result
    assert "pkg.mod2" in result
    assert "- SyntaxError: invalid syntax (line 42:10)" in result
    assert "- Missing top-level definition: foo" in result
    assert "- Dependency failed: pkg.mod1" in result
    # Modules should be sorted alphabetically.
    assert result.index("pkg.mod1") < result.index("pkg.mod2")


def test_format_build_failures_empty_returns_empty() -> None:
    assert format_build_failures({}) == ""


# --- format_test_generation_failures ---


def test_format_test_failures_single_module() -> None:
    result = format_test_generation_failures({"tests.test_foo": ["No source returned."]})
    assert "Test generation failed for 1 module(s):" in result
    assert "tests.test_foo" in result
    assert "- No source returned." in result


def test_format_test_failures_empty_returns_empty() -> None:
    assert format_test_generation_failures({}) == ""


# --- format_hint ---


def test_hint_missing_toml() -> None:
    exc = JauntConfigError("Could not find jaunt.toml by walking upward from start path.")
    hint = format_hint(exc)
    assert hint is not None
    assert "jaunt init" in hint


def test_hint_missing_api_key() -> None:
    exc = JauntConfigError(
        "Missing API key: OPENAI_API_KEY. Set it in the environment or add it to .env."
    )
    hint = format_hint(exc)
    assert hint is not None
    assert ".env" in hint


def test_hint_discovery_error() -> None:
    exc = JauntDiscoveryError(
        "Failed to import magic module 'foo': ModuleNotFoundError: No module named 'foo'"
    )
    hint = format_hint(exc)
    assert hint is not None
    assert "source_roots" in hint


def test_hint_cycle_error() -> None:
    exc = JauntDependencyCycleError("Dependency cycle detected: a -> b -> a")
    hint = format_hint(exc)
    assert hint is not None
    assert "deps=" in hint or "infer_deps" in hint


def test_hint_key_error() -> None:
    exc = KeyError("OPENAI_API_KEY")
    hint = format_hint(exc)
    assert hint is not None
    assert ".env" in hint


def test_hint_unsupported_provider_no_hint() -> None:
    exc = JauntConfigError("Unsupported llm.provider: 'bogus'. Supported: 'openai', 'anthropic'.")
    hint = format_hint(exc)
    assert hint is None


def test_hint_unknown_error_returns_none() -> None:
    exc = RuntimeError("something unexpected")
    assert format_hint(exc) is None


# --- format_error_with_hint ---


def test_error_with_hint_includes_both() -> None:
    exc = JauntConfigError("Could not find jaunt.toml by walking upward from start path.")
    result = format_error_with_hint(exc)
    assert "error:" in result
    assert "jaunt.toml" in result
    assert "hint:" in result
    assert "jaunt init" in result


def test_error_with_hint_no_hint() -> None:
    exc = RuntimeError("something unexpected")
    result = format_error_with_hint(exc)
    assert "error:" in result
    assert "something unexpected" in result
    assert "hint:" not in result


def test_error_with_hint_key_error_formatting() -> None:
    exc = KeyError("OPENAI_API_KEY")
    result = format_error_with_hint(exc)
    assert "error:" in result
    assert "OPENAI_API_KEY" in result
    assert "hint:" in result


def test_error_with_hint_empty_message_uses_repr() -> None:
    exc = JauntError("")
    result = format_error_with_hint(exc)
    assert "error:" in result
    # Should use repr fallback when str is empty.
    assert len(result.strip()) > len("error:")


# --- CLI integration ---


def test_cli_print_error_with_hint(capsys) -> None:
    exc = JauntConfigError("Could not find jaunt.toml by walking upward from start path.")
    jaunt.cli._print_error(exc)
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "jaunt.toml" in captured.err
    assert "hint:" in captured.err
    assert "jaunt init" in captured.err


def test_cli_print_error_no_hint(capsys) -> None:
    exc = RuntimeError("something")
    jaunt.cli._print_error(exc)
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "hint:" not in captured.err
