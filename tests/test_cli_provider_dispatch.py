"""Tests for CLI _build_backend provider dispatch."""

from __future__ import annotations

import pytest

from jaunt.config import (
    AgentConfig,
    AiderConfig,
    BuildConfig,
    JauntConfig,
    LLMConfig,
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
        agent=AgentConfig(engine="legacy"),
        aider=AiderConfig(),
    )


def _fake_aider_classes():
    return (
        type("Coder", (), {"create": staticmethod(lambda **_: object())}),
        type("IO", (), {}),
        type("Model", (), {}),
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


@pytest.mark.skipif(
    not pytest.importorskip("cerebras.cloud.sdk", reason="cerebras SDK not installed"),
    reason="cerebras SDK not installed",
)
def test_build_backend_cerebras(monkeypatch) -> None:
    from jaunt.cli import _build_backend

    monkeypatch.setenv("CEREBRAS_API_KEY", "test-key")
    backend = _build_backend(_cfg("cerebras", "CEREBRAS_API_KEY"))
    from jaunt.generate.cerebras_backend import CerebrasBackend

    assert isinstance(backend, CerebrasBackend)


def test_build_backend_unsupported() -> None:
    from jaunt.cli import _build_backend

    with pytest.raises(JauntConfigError, match="Unsupported llm.provider"):
        _build_backend(_cfg("unsupported-provider"))


def test_build_backend_aider(monkeypatch) -> None:
    from jaunt.cli import _build_backend
    from jaunt.generate.aider_backend import AiderGeneratorBackend

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    cfg = _cfg("openai")
    cfg = JauntConfig(
        version=cfg.version,
        paths=cfg.paths,
        llm=cfg.llm,
        build=cfg.build,
        test=cfg.test,
        prompts=cfg.prompts,
        agent=AgentConfig(engine="aider"),
        aider=AiderConfig(),
    )

    import jaunt.aider_executor as aider_executor

    monkeypatch.setattr(
        aider_executor.AiderExecutor,
        "_load_aider_classes",
        staticmethod(_fake_aider_classes),
    )
    backend = _build_backend(cfg)
    assert isinstance(backend, AiderGeneratorBackend)


def test_build_backend_aider_missing_dependency(monkeypatch) -> None:
    from jaunt.cli import _build_backend

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    cfg = _cfg("openai")
    cfg = JauntConfig(
        version=cfg.version,
        paths=cfg.paths,
        llm=cfg.llm,
        build=cfg.build,
        test=cfg.test,
        prompts=cfg.prompts,
        agent=AgentConfig(engine="aider"),
        aider=AiderConfig(),
    )

    import jaunt.aider_executor as aider_executor

    def _boom():
        raise JauntConfigError("The 'aider-chat' package is required for agent.engine='aider'.")

    monkeypatch.setattr(
        aider_executor.AiderExecutor,
        "_load_aider_classes",
        staticmethod(_boom),
    )
    with pytest.raises(JauntConfigError, match="aider-chat"):
        _build_backend(cfg)
