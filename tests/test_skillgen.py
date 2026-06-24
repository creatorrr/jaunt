"""Tests for OpenAI skill generator import guard."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from jaunt.agent_runtime import AgentTask
from jaunt.config import AgentConfig, CodexConfig, LLMConfig
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


def test_codex_skill_generator_returns_stripped_valid_markdown(monkeypatch) -> None:
    from jaunt import skillgen

    seen: dict[str, AgentTask] = {}

    async def fake_run_task(self, task):  # noqa: ANN001
        seen["task"] = task
        return SimpleNamespace(
            output="\n".join(
                [
                    "```markdown",
                    "# skill",
                    "## What it is",
                    "Generated.",
                    "## Core concepts",
                    "Concepts.",
                    "## Common patterns",
                    "Patterns.",
                    "## Gotchas",
                    "Gotchas.",
                    "## Testing notes",
                    "Testing.",
                    "```",
                ]
            )
        )

    monkeypatch.setattr(skillgen.CodexExecutor, "run_task", fake_run_task)

    gen = skillgen.CodexSkillGenerator(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        AgentConfig(engine="codex"),
        CodexConfig(),
    )
    result = asyncio.run(gen.generate_skill_markdown("dist", "1.0", "# README", "text/markdown"))

    assert result.startswith("# skill")
    assert "```" not in result
    assert seen["task"].kind == "pypi_skill_generate"
    assert seen["task"].mode == "code"
