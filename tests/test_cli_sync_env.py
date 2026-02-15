"""Tests for CLI _sync_generated_dir_env."""

from __future__ import annotations

import os


def test_sync_generated_dir_env_sets_default(monkeypatch) -> None:
    monkeypatch.delenv("JAUNT_GENERATED_DIR", raising=False)

    from jaunt.cli import _sync_generated_dir_env
    from jaunt.config import (
        BuildConfig,
        JauntConfig,
        LLMConfig,
        MCPConfig,
        PathsConfig,
        PromptsConfig,
        TestConfig,
    )

    cfg = JauntConfig(
        version=1,
        paths=PathsConfig(source_roots=["src"], test_roots=["tests"], generated_dir="custom_out"),
        llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        build=BuildConfig(jobs=1, infer_deps=True),
        test=TestConfig(jobs=1, infer_deps=True, pytest_args=[]),
        prompts=PromptsConfig(build_system="", build_module="", test_system="", test_module=""),
        mcp=MCPConfig(enabled=True),
    )

    _sync_generated_dir_env(cfg)
    assert os.environ.get("JAUNT_GENERATED_DIR") == "custom_out"

    # Clean up.
    monkeypatch.delenv("JAUNT_GENERATED_DIR", raising=False)


def test_sync_generated_dir_env_does_not_override(monkeypatch) -> None:
    monkeypatch.setenv("JAUNT_GENERATED_DIR", "already_set")

    from jaunt.cli import _sync_generated_dir_env
    from jaunt.config import (
        BuildConfig,
        JauntConfig,
        LLMConfig,
        MCPConfig,
        PathsConfig,
        PromptsConfig,
        TestConfig,
    )

    cfg = JauntConfig(
        version=1,
        paths=PathsConfig(source_roots=["src"], test_roots=["tests"], generated_dir="other"),
        llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        build=BuildConfig(jobs=1, infer_deps=True),
        test=TestConfig(jobs=1, infer_deps=True, pytest_args=[]),
        prompts=PromptsConfig(build_system="", build_module="", test_system="", test_module=""),
        mcp=MCPConfig(enabled=True),
    )

    _sync_generated_dir_env(cfg)
    # Should NOT override.
    assert os.environ.get("JAUNT_GENERATED_DIR") == "already_set"
