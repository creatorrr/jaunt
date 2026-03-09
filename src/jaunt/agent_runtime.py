"""Shared task/executor abstractions for agent-backed generation flows."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from jaunt.generate.base import TokenUsage

AgentTaskKind = Literal["build_module", "test_module", "skill_update", "pypi_skill_generate"]
AgentMode = Literal["architect", "code"]


@dataclass(frozen=True, slots=True)
class AgentFile:
    relative_path: str
    content: str


@dataclass(frozen=True, slots=True)
class AgentTask:
    kind: AgentTaskKind
    mode: AgentMode
    instruction: str
    target_file: AgentFile
    read_only_files: list[AgentFile] = field(default_factory=list)
    edit_format: str | None = None
    editor_edit_format: str | None = None
    main_reasoning_effort: str | None = None
    editor_reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTaskResult:
    output: str
    usage: TokenUsage | None = None
    trace_dir: Path | None = None


class AgentTaskExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        output: str = "",
        usage: TokenUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.output = output
        self.usage = usage


class AgentExecutor(ABC):
    @property
    def engine_name(self) -> str:
        return "agent"

    @abstractmethod
    async def run_task(self, task: AgentTask) -> AgentTaskResult:
        """Materialize *task*, execute it, and return the edited target content."""
