from __future__ import annotations

import asyncio

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

    async def fake_call(messages):
        assert isinstance(messages, list)
        return "```python\nprint('hi')\n```"

    monkeypatch.setattr(backend, "_call_openai", fake_call)
    out = asyncio.run(backend.generate_module(_ctx("build")))
    assert out == "print('hi')"


def test_openai_backend_renders_expected_names_and_kind_specific_rules(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )

    seen: list[list[dict[str, str]]] = []

    async def fake_call(messages):
        seen.append(messages)
        return "def foo():\n    return 1\n"

    monkeypatch.setattr(backend, "_call_openai", fake_call)

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


def test_openai_backend_errors_when_api_key_missing(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(JauntConfigError) as ei:
        OpenAIBackend(LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"))
    assert "Missing API key" in str(ei.value)
