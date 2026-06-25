"""Codex exec-backed shared executor for Jaunt agent tasks."""

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
from jaunt.generate.base import TokenUsage
from jaunt.generate.codex_backend import run_codex_exec


class CodexExecutor(AgentExecutor):
    def __init__(self, codex: CodexConfig, llm: LLMConfig) -> None:
        self._codex = codex
        self._llm = llm
        self._model = codex.model or llm.model

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

    def _usage_from(self, usage_input: int | None, usage_output: int | None) -> TokenUsage | None:
        if isinstance(usage_input, int) and isinstance(usage_output, int):
            return TokenUsage(
                prompt_tokens=usage_input,
                completion_tokens=usage_output,
                model=self._model,
                provider="codex",
            )
        return None

    async def run_task(self, task: AgentTask) -> AgentTaskResult:
        with tempfile.TemporaryDirectory(prefix="jaunt-codex-") as tmp:
            root = Path(tmp).resolve()
            target_path = self._write_workspace(root, task)
            prompt = self._build_prompt(task)
            try:
                result = await run_codex_exec(
                    prompt=prompt,
                    cwd=str(root),
                    sandbox=self._codex.sandbox,
                    model=self._model,
                    reasoning_effort=self._codex.reasoning_effort,
                    extra_config=dict(self._codex.config or {}),
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
                usage=self._usage_from(result.usage_input, result.usage_output),
                trace_dir=None,
            )

    async def aclose(self) -> None:
        return None
