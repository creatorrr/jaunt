from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from jaunt.agent_runtime import AgentFile, AgentTask, AgentTaskExecutionError
from jaunt.codex_executor import CodexExecutor
from jaunt.config import CodexConfig, LLMConfig
from jaunt.generate.base import TokenUsage


def _executor() -> CodexExecutor:
    return CodexExecutor(
        CodexConfig(),
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
    )


def test_codex_executor_writes_workspace_and_returns_target(monkeypatch) -> None:
    async def run() -> None:
        executor = _executor()
        seen: dict[str, object] = {}
        session = SimpleNamespace()

        async def call_tool(name, args):
            args = cast(dict[str, object], args)
            root = Path(cast(str, args["cwd"]))
            seen["name"] = name
            seen["prompt"] = args["prompt"]
            seen["target_seed"] = (root / "workspace/SKILL.md").read_text(encoding="utf-8")
            seen["read_only"] = (root / "context/readme.md").read_text(encoding="utf-8")
            (root / "workspace/SKILL.md").write_text("# skill\nUpdated.\n", encoding="utf-8")
            return SimpleNamespace(
                structuredContent={"usage": {"input_tokens": 10, "output_tokens": 5}}
            )

        session.call_tool = AsyncMock(side_effect=call_tool)
        monkeypatch.setattr(executor._backend, "_spawn_slot", AsyncMock(return_value=session))

        result = await executor.run_task(
            AgentTask(
                kind="skill_update",
                mode="code",
                instruction="Update the skill.",
                target_file=AgentFile(relative_path="workspace/SKILL.md", content="# old\n"),
                read_only_files=[
                    AgentFile(relative_path="context/readme.md", content="# README\n")
                ],
            )
        )

        assert seen["name"] == "codex"
        assert seen["target_seed"] == "# old\n"
        assert seen["read_only"] == "# README\n"
        prompt = seen["prompt"]
        assert isinstance(prompt, str)
        assert "workspace/SKILL.md" in prompt
        assert "context/readme.md" in prompt
        assert "skill_mode" not in prompt
        assert "architect" not in prompt
        assert "diff" not in prompt
        assert result.output == "# skill\nUpdated.\n"
        assert result.usage == TokenUsage(10, 5, model="gpt-test", provider="codex")

    asyncio.run(run())


def test_codex_executor_raises_execution_error_with_partial_output(monkeypatch) -> None:
    async def run() -> None:
        executor = _executor()
        session = SimpleNamespace()

        async def call_tool(name, args):
            args = cast(dict[str, object], args)
            root = Path(cast(str, args["cwd"]))
            (root / "workspace/SKILL.md").write_text("partial\n", encoding="utf-8")
            raise RuntimeError("boom")

        session.call_tool = AsyncMock(side_effect=call_tool)
        monkeypatch.setattr(executor._backend, "_spawn_slot", AsyncMock(return_value=session))

        with pytest.raises(AgentTaskExecutionError) as exc_info:
            await executor.run_task(
                AgentTask(
                    kind="skill_update",
                    mode="code",
                    instruction="Update the skill.",
                    target_file=AgentFile(relative_path="workspace/SKILL.md", content="# old\n"),
                    read_only_files=[],
                )
            )

        assert "boom" in str(exc_info.value)
        assert exc_info.value.output == "partial\n"
        assert exc_info.value.usage is None

    asyncio.run(run())
