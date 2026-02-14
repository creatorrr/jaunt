"""Tests for Anthropic backend."""

from __future__ import annotations

import asyncio
import sys

import pytest

from jaunt.config import LLMConfig
from jaunt.errors import JauntConfigError
from jaunt.generate.base import ModuleSpecContext

anthropic = pytest.importorskip("anthropic", reason="anthropic SDK not installed")


def _ctx() -> ModuleSpecContext:
    return ModuleSpecContext(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["foo"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )


def test_anthropic_backend_errors_when_api_key_missing(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(JauntConfigError, match="Missing API key"):
        from jaunt.generate.anthropic_backend import AnthropicBackend

        AnthropicBackend(
            LLMConfig(provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY")
        )


def test_anthropic_backend_errors_when_package_missing(monkeypatch) -> None:
    """If anthropic SDK is not installed, a clear error is raised."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    # Temporarily make anthropic un-importable.
    original = sys.modules.get("anthropic")
    sys.modules["anthropic"] = None  # type: ignore[assignment]

    try:
        with pytest.raises(JauntConfigError, match="'anthropic' package is required"):
            from jaunt.generate.anthropic_backend import AnthropicBackend

            AnthropicBackend(
                LLMConfig(
                    provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY"
                )
            )
    finally:
        if original is not None:
            sys.modules["anthropic"] = original
        else:
            sys.modules.pop("anthropic", None)


def test_anthropic_backend_strips_fences(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY")
    )

    async def fake_call(system, messages):
        return "```python\ndef foo():\n    return 42\n```"

    monkeypatch.setattr(backend, "_call_anthropic", fake_call)

    result = asyncio.run(backend.generate_module(_ctx()))
    assert "def foo" in result
    assert "```" not in result


def test_anthropic_backend_render_messages_structure(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY")
    )

    system, messages = backend._render_messages(_ctx(), extra_error_context=None)

    # System is a string containing the system prompt.
    assert isinstance(system, str)
    assert "foo" in system

    # Messages should have at least one user message.
    assert len(messages) >= 1
    assert messages[-1]["role"] == "user"
