"""Tests for CLI _build_backend provider dispatch."""

from __future__ import annotations

import pytest

from jaunt.config import (
    BuildConfig,
    JauntConfig,
    LLMConfig,
    MCPConfig,
    PathsConfig,
    PromptsConfig,
    TestConfig,
)
from jaunt.errors import JauntConfigError


def _cfg(provider: str, api_key_env: str = "OPENAI_API_KEY") -> JauntConfig:
    return JauntConfig(
        version=1,
        paths=PathsConfig(
            source_roots=["src"], test_roots=["tests"], generated_dir="__generated__"
        ),
        llm=LLMConfig(provider=provider, model="test-model", api_key_env=api_key_env),
        build=BuildConfig(jobs=1, infer_deps=True),
        test=TestConfig(jobs=1, infer_deps=True, pytest_args=[]),
        prompts=PromptsConfig(build_system="", build_module="", test_system="", test_module=""),
        mcp=MCPConfig(enabled=True),
    )


def test_build_backend_openai(monkeypatch) -> None:
    from jaunt.cli import _build_backend
    from jaunt.generate.openai_backend import OpenAIBackend

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    backend = _build_backend(_cfg("openai"))
    assert isinstance(backend, OpenAIBackend)


@pytest.mark.skipif(
    not pytest.importorskip("anthropic", reason="anthropic SDK not installed"),
    reason="anthropic SDK not installed",
)
def test_build_backend_anthropic(monkeypatch) -> None:
    from jaunt.cli import _build_backend

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    backend = _build_backend(_cfg("anthropic", "ANTHROPIC_API_KEY"))
    # Import here to check type.
    from jaunt.generate.anthropic_backend import AnthropicBackend

    assert isinstance(backend, AnthropicBackend)


def test_build_backend_unsupported() -> None:
    from jaunt.cli import _build_backend

    with pytest.raises(JauntConfigError, match="Unsupported llm.provider"):
        _build_backend(_cfg("unsupported-provider"))
