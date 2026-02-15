"""Tests for OpenAI skill generator import guard."""

from __future__ import annotations

import sys

import pytest

from jaunt.config import LLMConfig
from jaunt.errors import JauntConfigError


def test_skillgen_errors_when_openai_package_missing(monkeypatch) -> None:
    """If openai SDK is not installed, OpenAISkillGenerator raises JauntConfigError."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    original = sys.modules.get("openai")
    sys.modules["openai"] = None  # type: ignore[assignment]

    try:
        with pytest.raises(JauntConfigError, match="'openai' package is required"):
            from jaunt.skillgen import OpenAISkillGenerator

            OpenAISkillGenerator(
                LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
            )
    finally:
        if original is not None:
            sys.modules["openai"] = original
        else:
            sys.modules.pop("openai", None)
