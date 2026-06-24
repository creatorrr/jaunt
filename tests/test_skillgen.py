"""Tests for OpenAI skill generator import guard."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from jaunt.agent_runtime import AgentTask
from jaunt.config import AgentConfig, CodexConfig, LLMConfig


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
