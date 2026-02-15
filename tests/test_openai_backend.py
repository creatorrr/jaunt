from __future__ import annotations

import asyncio
import json
import logging
import sys

import pytest

from jaunt.config import LLMConfig
from jaunt.errors import JauntConfigError
from jaunt.generate.base import ModuleSpecContext
from jaunt.generate.openai_backend import OpenAIBackend


def _ctx(kind: str) -> ModuleSpecContext:
    return ModuleSpecContext(
        kind=kind,  # type: ignore[arg-type]
        spec_module="pkg.specs",
        generated_module="__generated__.pkg.specs",
        expected_names=["foo", "BAR"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )


def test_openai_backend_strips_fences(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    # Force fallback (non-structured) path.
    monkeypatch.setattr(type(backend), "supports_structured_output", property(lambda self: False))

    async def fake_call(messages):
        assert isinstance(messages, list)
        return "```python\nprint('hi')\n```", None

    monkeypatch.setattr(backend, "_call_openai", fake_call)
    source, usage = asyncio.run(backend.generate_module(_ctx("build")))
    assert source == "print('hi')"


def test_openai_backend_renders_expected_names_and_kind_specific_rules(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    seen: list[list[dict[str, str]]] = []

    async def fake_call(messages):
        seen.append(messages)
        return "def foo():\n    return 1\n", None

    # Uses structured path by default — mock it.
    monkeypatch.setattr(backend, "_call_openai_structured", fake_call)

    asyncio.run(backend.generate_module(_ctx("build")))
    asyncio.run(backend.generate_module(_ctx("test")))

    assert len(seen) == 2
    build_msgs = seen[0]
    test_msgs = seen[1]

    build_user = build_msgs[1]["content"]
    test_user = test_msgs[1]["content"]
    build_system = build_msgs[0]["content"]
    test_system = test_msgs[0]["content"]

    # Names should appear in rendered prompts.
    for blob in (build_user, test_user, build_system, test_system):
        assert "foo" in blob
        assert "BAR" in blob

    # Build prompts: must not generate tests.
    assert ("Do not write tests" in build_system) or ("Do not generate tests" in build_user)

    # Test prompts: must generate tests only.
    assert ("Generate tests only" in test_user) or ("tests only" in test_system)
    assert "Do not guess" in test_user


def test_openai_backend_errors_when_package_missing(monkeypatch) -> None:
    """If openai SDK is not installed, a clear JauntConfigError is raised."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    original = sys.modules.get("openai")
    sys.modules["openai"] = None  # type: ignore[assignment]

    try:
        with pytest.raises(JauntConfigError, match="'openai' package is required"):
            OpenAIBackend(
                LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
            )
    finally:
        if original is not None:
            sys.modules["openai"] = original
        else:
            sys.modules.pop("openai", None)


def test_openai_backend_errors_when_api_key_missing(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(JauntConfigError) as ei:
        OpenAIBackend(LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"))
    assert "Missing API key" in str(ei.value)


def test_openai_backend_injects_skills_block_as_extra_user_message(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    seen: list[list[dict[str, str]]] = []

    async def fake_call(messages):
        seen.append(messages)
        return "def foo():\n    return 1\n", None

    # Uses structured path by default — mock it.
    monkeypatch.setattr(backend, "_call_openai_structured", fake_call)

    ctx = ModuleSpecContext(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["foo"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
        skills_block="## requests==2.0.0\nUse requests.get(...)\n",
    )

    source, usage = asyncio.run(backend.generate_module(ctx))
    assert "def foo" in source
    assert len(seen) == 1
    msgs = seen[0]
    assert len(msgs) == 3
    assert msgs[1]["role"] == "user"
    assert "External library skills (reference):" in msgs[1]["content"]


# -- Structured output tests --


def test_openai_backend_supports_structured_output(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )
    assert backend.supports_structured_output is True


def test_openai_generate_module_uses_structured_output(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    structured_called: list[list[dict[str, str]]] = []

    async def fake_structured_call(messages):
        structured_called.append(messages)
        return "def foo():\n    return 42\n", None

    monkeypatch.setattr(backend, "_call_openai_structured", fake_structured_call)

    source, usage = asyncio.run(backend.generate_module(_ctx("build")))
    assert source == "def foo():\n    return 42\n"
    assert len(structured_called) == 1


def test_openai_generate_module_fallback_when_structured_disabled(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    monkeypatch.setattr(type(backend), "supports_structured_output", property(lambda self: False))

    async def fake_call(messages):
        return "```python\ndef foo():\n    return 99\n```", None

    monkeypatch.setattr(backend, "_call_openai", fake_call)

    source, usage = asyncio.run(backend.generate_module(_ctx("build")))
    assert source == "def foo():\n    return 99"


def test_openai_call_structured_sends_response_format(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    captured_kwargs: list[dict] = []

    class _FakeResp:
        class _Choice:
            class _Message:
                content = '{"python_source": "def foo():\\n    return 1\\n", "imports_used": []}'

            message = _Message()

        choices = [_Choice()]

    class _FakeCompletions:
        @staticmethod
        async def create(**kwargs):
            captured_kwargs.append(kwargs)
            return _FakeResp()

    monkeypatch.setattr(
        backend,
        "_client",
        type("C", (), {"chat": type("Ch", (), {"completions": _FakeCompletions})()})(),
    )

    messages = [{"role": "user", "content": "generate code"}]
    source, usage = asyncio.run(backend._call_openai_structured(messages))

    assert source == "def foo():\n    return 1\n"
    assert len(captured_kwargs) == 1
    assert "response_format" in captured_kwargs[0]
    rf = captured_kwargs[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "module_output"
    schema = rf["json_schema"]["schema"]
    assert "python_source" in schema["properties"]
    assert "imports_used" in schema["properties"]
    assert schema["required"] == ["python_source"]


def test_openai_structured_json_parse_error_raises(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    class _FakeResp:
        class _Choice:
            class _Message:
                content = "this is not json"

            message = _Message()

        choices = [_Choice()]

    class _FakeCompletions:
        @staticmethod
        async def create(**kwargs):
            return _FakeResp()

    monkeypatch.setattr(
        backend,
        "_client",
        type("C", (), {"chat": type("Ch", (), {"completions": _FakeCompletions})()})(),
    )
    import jaunt.generate.openai_backend as mod

    monkeypatch.setattr(mod, "_BASE_BACKOFF_S", 0.001)

    with pytest.raises(json.JSONDecodeError):
        asyncio.run(backend._call_openai_structured([{"role": "user", "content": "hi"}]))


def test_openai_structured_missing_python_source_raises(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    class _FakeResp:
        class _Choice:
            class _Message:
                content = '{"imports_used": ["os"]}'

            message = _Message()

        choices = [_Choice()]

    class _FakeCompletions:
        @staticmethod
        async def create(**kwargs):
            return _FakeResp()

    monkeypatch.setattr(
        backend,
        "_client",
        type("C", (), {"chat": type("Ch", (), {"completions": _FakeCompletions})()})(),
    )
    import jaunt.generate.openai_backend as mod

    monkeypatch.setattr(mod, "_BASE_BACKOFF_S", 0.001)

    with pytest.raises(KeyError):
        asyncio.run(backend._call_openai_structured([{"role": "user", "content": "hi"}]))


def test_openai_structured_logs_imports_used(monkeypatch, caplog) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    class _FakeResp:
        class _Choice:
            class _Message:
                content = '{"python_source": "import os\\n", "imports_used": ["os", "sys"]}'

            message = _Message()

        choices = [_Choice()]

    class _FakeCompletions:
        @staticmethod
        async def create(**kwargs):
            return _FakeResp()

    monkeypatch.setattr(
        backend,
        "_client",
        type("C", (), {"chat": type("Ch", (), {"completions": _FakeCompletions})()})(),
    )

    with caplog.at_level(logging.DEBUG, logger="jaunt.generate.openai"):
        source, usage = asyncio.run(
            backend._call_openai_structured([{"role": "user", "content": "hi"}])
        )

    assert source == "import os\n"
    assert "imports_used" in caplog.text
