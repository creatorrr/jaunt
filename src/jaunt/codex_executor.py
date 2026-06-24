"""Codex-backed shared executor for Jaunt agent tasks."""

from __future__ import annotations

import tempfile
from pathlib import Path

from jaunt.agent_runtime import (
    AgentExecutor,
    AgentTask,
    AgentTaskExecutionError,
    AgentTaskResult,
)
from jaunt.config import CodexConfig, LLMConfig
from jaunt.generate.codex_backend import CodexBackend


class CodexExecutor(AgentExecutor):
    def __init__(self, codex: CodexConfig, llm: LLMConfig) -> None:
        self._codex = codex
        self._llm = llm
        self._model = codex.model or llm.model
        self._backend = CodexBackend(codex, llm, pool_size=1)

    @property
    def engine_name(self) -> str:
        return "codex"

    @staticmethod
    def _write_workspace(root: Path, task: AgentTask) -> Path:
        target_path = root / task.target_file.relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(task.target_file.content, encoding="utf-8")

        for ro_file in task.read_only_files:
            ro_path = root / ro_file.relative_path
            ro_path.parent.mkdir(parents=True, exist_ok=True)
            ro_path.write_text(ro_file.content, encoding="utf-8")

        return target_path

    @staticmethod
    def _build_prompt(task: AgentTask) -> str:
        context_paths = [f"- `{ro_file.relative_path}`" for ro_file in task.read_only_files]
        if not context_paths:
            context_block = "- none"
        else:
            context_block = "\n".join(context_paths)

        return "\n\n".join(
            [
                f"Edit ONLY the target file `{task.target_file.relative_path}`.",
                "Write the full completed output to that target file.",
                "Do not modify any other file.",
                "Instruction:\n" + task.instruction.strip(),
                "Read-only reference files:\n" + context_block,
            ]
        )

    async def run_task(self, task: AgentTask) -> AgentTaskResult:
        session = await self._backend._checkout()
        try:
            with tempfile.TemporaryDirectory(prefix="jaunt-codex-") as tmp:
                root = Path(tmp).resolve()
                target_path = self._write_workspace(root, task)
                prompt = self._build_prompt(task)
                try:
                    res = await session.call_tool(
                        "codex",
                        {
                            "prompt": prompt,
                            "cwd": str(root),
                            "sandbox": self._codex.sandbox,
                            "approval-policy": "never",
                            "model": self._model,
                            "config": {
                                "model_reasoning_effort": self._codex.reasoning_effort,
                                **(self._codex.config or {}),
                            },
                        },
                    )
                except Exception as e:
                    output = ""
                    try:
                        if target_path.exists():
                            output = target_path.read_text(encoding="utf-8")
                    except Exception:
                        output = ""
                    raise AgentTaskExecutionError(str(e), output=output, usage=None) from e

                output = target_path.read_text(encoding="utf-8")
                return AgentTaskResult(
                    output=output,
                    usage=self._backend._extract_usage(res),
                    trace_dir=None,
                )
        finally:
            self._backend._return(session)

    async def aclose(self) -> None:
        await self._backend.aclose()
