"""Aider-backed shared executor for Jaunt agent tasks."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from jaunt.agent_runtime import (
    AgentExecutor,
    AgentTask,
    AgentTaskExecutionError,
    AgentTaskResult,
)
from jaunt.config import AiderConfig, LLMConfig
from jaunt.errors import JauntConfigError
from jaunt.generate.base import TokenUsage

# Current limitation: when llm.api_key_env differs from the provider's canonical
# env var, Aider/litellm still expects the canonical name. Jaunt remaps the key
# through os.environ under a process-wide lock to avoid credential races, which
# also serializes those tasks. Follow-up scope: pass the key directly into the
# Aider/litellm model path so parallel jobs can stay fully concurrent.
_API_KEY_ENV_LOCK = threading.Lock()


class AiderExecutor(AgentExecutor):
    def __init__(self, llm: LLMConfig, aider: AiderConfig) -> None:
        self._llm = llm
        self._aider = aider
        self._coder_cls, self._io_cls, self._model_cls = self._load_aider_classes()
        self._validate_api_key_env()

    @property
    def engine_name(self) -> str:
        return "aider"

    @staticmethod
    def _load_aider_classes() -> tuple[Any, Any, Any]:
        try:
            from aider.coders import Coder
            from aider.io import InputOutput
            from aider.models import Model
        except ImportError as e:
            raise JauntConfigError(
                "The 'aider-chat' package is required for agent.engine='aider'. "
                "Install a provider extra such as: pip install jaunt[openai]"
            ) from e

        return Coder, InputOutput, Model

    @staticmethod
    def _provider_env_var(provider: str) -> str | None:
        if provider == "openai":
            return "OPENAI_API_KEY"
        if provider == "anthropic":
            return "ANTHROPIC_API_KEY"
        if provider == "cerebras":
            return "CEREBRAS_API_KEY"
        return None

    @staticmethod
    def _model_name(llm: LLMConfig, model: str) -> str:
        raw = model.strip()
        if "/" in raw:
            return raw
        if llm.provider in {"openai", "anthropic", "cerebras"}:
            return f"{llm.provider}/{raw}"
        return raw

    def _validate_api_key_env(self) -> None:
        source_name = self._llm.api_key_env
        source_value = (os.environ.get(source_name) or "").strip()
        if not source_value:
            raise JauntConfigError(
                f"Missing API key: {source_name}. "
                f"Set it in the environment or add it to <project_root>/.env."
            )

    def _resolve_api_key_env(self) -> tuple[str, str] | None:
        source_name = self._llm.api_key_env
        source_value = (os.environ.get(source_name) or "").strip()
        if not source_value:
            raise JauntConfigError(
                f"Missing API key: {source_name}. "
                f"Set it in the environment or add it to <project_root>/.env."
            )

        target_name = self._provider_env_var(self._llm.provider)
        if target_name is None or target_name == source_name:
            return None

        return target_name, source_value

    @contextlib.contextmanager
    def _mapped_api_key_env(self, *, target_name: str, source_value: str):
        old = os.environ.get(target_name)
        os.environ[target_name] = source_value
        try:
            yield
        finally:
            if old is None:
                os.environ.pop(target_name, None)
            else:
                os.environ[target_name] = old

    @contextlib.asynccontextmanager
    async def _api_key_env_scope(self):
        mapping = self._resolve_api_key_env()
        if mapping is None:
            yield
            return

        target_name, source_value = mapping
        await asyncio.to_thread(_API_KEY_ENV_LOCK.acquire)
        try:
            with self._mapped_api_key_env(
                target_name=target_name,
                source_value=source_value,
            ):
                yield
        finally:
            _API_KEY_ENV_LOCK.release()

    @staticmethod
    def _edit_format_for_mode(mode: str) -> str:
        if mode == "architect":
            return "architect"
        return "diff"

    def _make_workspace_dir(self) -> tuple[Path, Any]:
        if self._aider.save_traces:
            trace_dir = Path(tempfile.mkdtemp(prefix="jaunt-aider-trace-")).resolve()
            return trace_dir, None

        tmp = tempfile.TemporaryDirectory(prefix="jaunt-aider-")
        return Path(tmp.name).resolve(), tmp

    def _write_workspace(self, root: Path, task: AgentTask) -> tuple[Path, list[Path]]:
        target_path = root / task.target_file.relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(task.target_file.content, encoding="utf-8")

        ro_paths: list[Path] = []
        for ro_file in task.read_only_files:
            ro_path = root / ro_file.relative_path
            ro_path.parent.mkdir(parents=True, exist_ok=True)
            ro_path.write_text(ro_file.content, encoding="utf-8")
            ro_paths.append(ro_path)

        return target_path, ro_paths

    def _build_coder(
        self,
        root: Path,
        task: AgentTask,
        target_path: Path,
        ro_paths: list[Path],
    ) -> Any:
        model_kwargs: dict[str, Any] = {}
        if task.mode == "architect":
            editor_model = self._aider.editor_model.strip() or self._llm.model
            model_kwargs["editor_model"] = self._model_name(self._llm, editor_model)
            model_kwargs["editor_edit_format"] = task.editor_edit_format or "editor-diff"

        model = self._model_cls(self._model_name(self._llm, self._llm.model), **model_kwargs)
        self._apply_reasoning_effort(
            model,
            main_effort=task.main_reasoning_effort,
            editor_effort=task.editor_reasoning_effort,
        )
        io = self._io_cls(yes=True, pretty=False, fancy_input=False)

        return self._coder_cls.create(
            main_model=model,
            edit_format=task.edit_format or self._edit_format_for_mode(task.mode),
            io=io,
            fnames=[str(target_path)],
            read_only_fnames=[str(p) for p in ro_paths],
            auto_commits=False,
            auto_lint=False,
            auto_test=False,
            use_git=False,
            stream=False,
            map_tokens=self._aider.map_tokens,
            suggest_shell_commands=False,
            verbose=False,
        )

    def _apply_reasoning_effort(
        self,
        model: Any,
        *,
        main_effort: str | None,
        editor_effort: str | None,
    ) -> None:
        resolved_main = (
            main_effort if main_effort is not None else (self._llm.reasoning_effort or "")
        ).strip()
        if resolved_main:
            self._set_model_reasoning_effort(model, resolved_main)

        editor_model = getattr(model, "editor_model", None)
        if editor_model is not None and editor_model is not model:
            resolved_editor = (
                editor_effort if editor_effort is not None else resolved_main
            ).strip()
            if resolved_editor:
                self._set_model_reasoning_effort(editor_model, resolved_editor)

    @staticmethod
    def _set_model_reasoning_effort(model: Any, effort: str) -> None:
        setter = getattr(model, "set_reasoning_effort", None)
        if callable(setter):
            setter(effort)
            return

        # Fallback for test doubles or older aider builds that expose a model
        # object but not the helper method.
        extra_params = dict(getattr(model, "extra_params", None) or {})
        extra_body = dict(extra_params.get("extra_body", None) or {})
        extra_body["reasoning_effort"] = effort
        extra_params["extra_body"] = extra_body
        model.extra_params = extra_params

    @staticmethod
    def _usage_from_coder(coder: Any, llm: LLMConfig) -> TokenUsage | None:
        prompt_tokens = int(getattr(coder, "total_tokens_sent", 0) or 0)
        completion_tokens = int(getattr(coder, "total_tokens_received", 0) or 0)
        if prompt_tokens == 0 and completion_tokens == 0:
            return None
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=llm.model,
            provider=llm.provider,
        )

    async def run_task(self, task: AgentTask) -> AgentTaskResult:
        async with self._api_key_env_scope():
            root, tmp = self._make_workspace_dir()
            try:
                target_path, ro_paths = self._write_workspace(root, task)
                coder = self._build_coder(root, task, target_path, ro_paths)
                try:
                    await asyncio.to_thread(coder.run, task.instruction)
                except Exception as e:
                    output = ""
                    try:
                        if target_path.exists():
                            output = target_path.read_text(encoding="utf-8")
                    except Exception:
                        output = ""
                    raise AgentTaskExecutionError(
                        str(e),
                        output=output,
                        usage=self._usage_from_coder(coder, self._llm),
                    ) from e

                output = target_path.read_text(encoding="utf-8")
                return AgentTaskResult(
                    output=output,
                    usage=self._usage_from_coder(coder, self._llm),
                    trace_dir=root if self._aider.save_traces else None,
                )
            finally:
                if tmp is not None:
                    tmp.cleanup()
