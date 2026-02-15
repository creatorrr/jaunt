"""Tests for Anthropic backend."""

from __future__ import annotations

import asyncio
import logging
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

    # Force fallback (non-structured) path.
    monkeypatch.setattr(type(backend), "supports_structured_output", property(lambda self: False))

    async def fake_call(system, messages):
        return "```python\ndef foo():\n    return 42\n```", None

    monkeypatch.setattr(backend, "_call_anthropic", fake_call)

    source, usage = asyncio.run(backend.generate_module(_ctx()))
    assert "def foo" in source
    assert "```" not in source


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


# -- Structured output tests --


def test_anthropic_backend_supports_structured_output(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY")
    )
    assert backend.supports_structured_output is True


def test_anthropic_generate_module_uses_structured_output(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY")
    )

    structured_called: list[tuple] = []

    async def fake_structured_call(system, messages):
        structured_called.append((system, messages))
        return "def foo():\n    return 42\n", None

    monkeypatch.setattr(backend, "_call_anthropic_structured", fake_structured_call)

    source, usage = asyncio.run(backend.generate_module(_ctx()))
    assert source == "def foo():\n    return 42\n"
    assert len(structured_called) == 1


def test_anthropic_generate_module_fallback_when_structured_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY")
    )

    monkeypatch.setattr(type(backend), "supports_structured_output", property(lambda self: False))

    async def fake_call(system, messages):
        return "```python\ndef foo():\n    return 99\n```", None

    monkeypatch.setattr(backend, "_call_anthropic", fake_call)

    source, usage = asyncio.run(backend.generate_module(_ctx()))
    assert source == "def foo():\n    return 99"
    assert "```" not in source


def test_anthropic_call_structured_sends_tools_and_extracts_source(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY")
    )

    captured_kwargs: list[dict] = []

    class _FakeToolUseBlock:
        type = "tool_use"
        name = "write_module"
        input = {
            "python_source": "def foo():\n    return 1\n",
            "imports_used": ["os"],
            "notes": "simple function",
        }

    class _FakeResp:
        content = [_FakeToolUseBlock()]
        stop_reason = "tool_use"

    async def fake_create(**kwargs):
        captured_kwargs.append(kwargs)
        return _FakeResp()

    monkeypatch.setattr(
        backend,
        "_client",
        type("C", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})(),
    )

    source, usage = asyncio.run(
        backend._call_anthropic_structured("system prompt", [{"role": "user", "content": "hi"}])
    )

    assert source == "def foo():\n    return 1\n"
    assert len(captured_kwargs) == 1
    assert "tools" in captured_kwargs[0]
    tools = captured_kwargs[0]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "write_module"
    assert "python_source" in tools[0]["input_schema"]["properties"]
    assert "imports_used" in tools[0]["input_schema"]["properties"]
    assert "notes" in tools[0]["input_schema"]["properties"]
    assert tools[0]["input_schema"]["required"] == ["python_source"]
    # tool_choice forces the model to use the tool.
    assert captured_kwargs[0]["tool_choice"] == {"type": "tool", "name": "write_module"}
    assert "thinking" not in captured_kwargs[0]


def test_anthropic_call_structured_includes_thinking_when_budget_set(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(
            provider="anthropic",
            model="claude-test",
            api_key_env="ANTHROPIC_API_KEY",
            anthropic_thinking_budget_tokens=1536,
        )
    )

    captured_kwargs: list[dict] = []

    class _FakeToolUseBlock:
        type = "tool_use"
        name = "write_module"
        input = {"python_source": "def foo():\n    return 1\n"}

    class _FakeResp:
        content = [_FakeToolUseBlock()]
        stop_reason = "tool_use"

    async def fake_create(**kwargs):
        captured_kwargs.append(kwargs)
        return _FakeResp()

    monkeypatch.setattr(
        backend,
        "_client",
        type("C", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})(),
    )

    source, usage = asyncio.run(
        backend._call_anthropic_structured("system prompt", [{"role": "user", "content": "hi"}])
    )

    assert source == "def foo():\n    return 1\n"
    assert captured_kwargs[0]["thinking"] == {"type": "enabled", "budget_tokens": 1536}


def test_anthropic_call_includes_thinking_when_budget_set(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(
            provider="anthropic",
            model="claude-test",
            api_key_env="ANTHROPIC_API_KEY",
            anthropic_thinking_budget_tokens=1024,
        )
    )

    captured_kwargs: list[dict] = []

    class _FakeTextBlock:
        text = "def foo():\n    return 1\n"

    class _FakeResp:
        content = [_FakeTextBlock()]

    async def fake_create(**kwargs):
        captured_kwargs.append(kwargs)
        return _FakeResp()

    monkeypatch.setattr(
        backend,
        "_client",
        type("C", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})(),
    )

    source, usage = asyncio.run(
        backend._call_anthropic("system", [{"role": "user", "content": "hi"}])
    )

    assert source == "def foo():\n    return 1\n"
    assert captured_kwargs[0]["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def test_anthropic_call_omits_thinking_when_budget_unset(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY")
    )

    captured_kwargs: list[dict] = []

    class _FakeTextBlock:
        text = "def foo():\n    return 1\n"

    class _FakeResp:
        content = [_FakeTextBlock()]

    async def fake_create(**kwargs):
        captured_kwargs.append(kwargs)
        return _FakeResp()

    monkeypatch.setattr(
        backend,
        "_client",
        type("C", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})(),
    )

    source, usage = asyncio.run(
        backend._call_anthropic("system", [{"role": "user", "content": "hi"}])
    )

    assert source == "def foo():\n    return 1\n"
    assert "thinking" not in captured_kwargs[0]


def test_anthropic_structured_no_tool_use_block_raises(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY")
    )

    class _FakeTextBlock:
        type = "text"
        text = "I cannot generate that."

    class _FakeResp:
        content = [_FakeTextBlock()]
        stop_reason = "end_turn"

    async def fake_create(**kwargs):
        return _FakeResp()

    monkeypatch.setattr(
        backend,
        "_client",
        type("C", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})(),
    )

    import jaunt.generate.anthropic_backend as mod

    monkeypatch.setattr(mod, "_BASE_BACKOFF_S", 0.001)

    with pytest.raises(RuntimeError, match="write_module tool_use block"):
        asyncio.run(
            backend._call_anthropic_structured("system", [{"role": "user", "content": "hi"}])
        )


def test_anthropic_structured_retries_on_transient_error(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY")
    )

    calls: list[dict] = []

    class _FakeToolUseBlock:
        type = "tool_use"
        name = "write_module"
        input = {"python_source": "def foo(): pass\n"}

    class _FakeResp:
        content = [_FakeToolUseBlock()]
        stop_reason = "tool_use"

    class _FakeRateLimitError(Exception):
        pass

    _FakeRateLimitError.__name__ = "RateLimitError"

    async def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _FakeRateLimitError("rate limited")
        return _FakeResp()

    monkeypatch.setattr(
        backend,
        "_client",
        type("C", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})(),
    )

    import jaunt.generate.anthropic_backend as mod

    monkeypatch.setattr(mod, "_BASE_BACKOFF_S", 0.001)

    source, usage = asyncio.run(
        backend._call_anthropic_structured("system", [{"role": "user", "content": "hi"}])
    )
    assert source == "def foo(): pass\n"
    assert len(calls) == 2


def test_anthropic_structured_logs_imports_and_notes(monkeypatch, caplog) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from jaunt.generate.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(
        LLMConfig(provider="anthropic", model="claude-test", api_key_env="ANTHROPIC_API_KEY")
    )

    class _FakeToolUseBlock:
        type = "tool_use"
        name = "write_module"
        input = {
            "python_source": "import os\n",
            "imports_used": ["os"],
            "notes": "simple import",
        }

    class _FakeResp:
        content = [_FakeToolUseBlock()]
        stop_reason = "tool_use"

    async def fake_create(**kwargs):
        return _FakeResp()

    monkeypatch.setattr(
        backend,
        "_client",
        type("C", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})(),
    )

    with caplog.at_level(logging.DEBUG, logger="jaunt.generate.anthropic"):
        source, usage = asyncio.run(
            backend._call_anthropic_structured("system", [{"role": "user", "content": "hi"}])
        )

    assert source == "import os\n"
    assert "imports_used" in caplog.text
    assert "notes" in caplog.text
