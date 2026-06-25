from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest

from jaunt.agent_runtime import AgentFile, AgentTask, AgentTaskExecutionError
from jaunt.codex_executor import CodexExecutor
from jaunt.config import CodexConfig, LLMConfig
from jaunt.generate.base import TokenUsage


def _executor() -> CodexExecutor:
    return CodexExecutor(
        CodexConfig(model="gpt-test"),
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
    )


class _FakeProc:
    def __init__(
        self, stdout: bytes, returncode: int = 0, captured: dict[str, object] | None = None
    ) -> None:
        self._stdout = stdout
        self.returncode = returncode
        self._captured = captured

    async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
        if self._captured is not None:
            self._captured["prompt"] = (stdin or b"").decode("utf-8")
        return self._stdout, b""


def _usage_jsonl(input_tokens: int, output_tokens: int) -> bytes:
    lines = [
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            }
        ),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _cwd_from_args(args: list[str]) -> Path:
    idx = args.index("-C")
    return Path(args[idx + 1])


def test_codex_executor_writes_workspace_and_returns_target(monkeypatch) -> None:
    async def run() -> None:
        executor = _executor()
        seen: dict[str, object] = {}

        async def fake_exec(*args, **kwargs):
            args_list = list(args)
            root = _cwd_from_args(args_list)
            seen["args"] = args_list
            seen["target_seed"] = (root / "workspace/SKILL.md").read_text(encoding="utf-8")
            seen["read_only"] = (root / "context/readme.md").read_text(encoding="utf-8")
            (root / "workspace/SKILL.md").write_text("# skill\nUpdated.\n", encoding="utf-8")
            return _FakeProc(_usage_jsonl(10, 5), captured=seen)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

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

        args = cast(list[str], seen["args"])
        assert args[0] == "codex"
        assert args[1] == "exec"
        assert "--skip-git-repo-check" in args
        assert seen["target_seed"] == "# old\n"
        assert seen["read_only"] == "# README\n"
        prompt = cast(str, seen["prompt"])
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

        async def fake_exec(*args, **kwargs):
            root = _cwd_from_args(list(args))
            (root / "workspace/SKILL.md").write_text("partial\n", encoding="utf-8")
            raise RuntimeError("boom")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

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
