"""LLM-powered skill elaboration: reads package source files and updates SKILL.md."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jaunt.agent_runtime import AgentFile, AgentTask
from jaunt.aider_executor import AiderExecutor
from jaunt.config import AgentConfig, AiderConfig
from jaunt.errors import JauntConfigError
from jaunt.generate.shared import load_prompt, render_template
from jaunt.skill_agent import strip_markdown_fences, validate_skill_markdown

if TYPE_CHECKING:
    from jaunt.config import LLMConfig
    from jaunt.lib_inspect import LibContent


class SkillBuilder:
    """Reads package files and uses LLM to elaborate a skill."""

    def __init__(
        self,
        llm: LLMConfig,
        agent: AgentConfig | None = None,
        aider: AiderConfig | None = None,
    ) -> None:
        self._llm = llm
        self._agent = agent or AgentConfig()
        self._aider = aider or AiderConfig()
        self._model = llm.model
        self._provider = llm.provider
        self._system_prompt = load_prompt("skill_build_system.md", None)
        self._user_prompt = load_prompt("skill_build_user.md", None)

        if self._agent.engine == "aider":
            self._executor = AiderExecutor(llm, self._aider)
            self._client = None
            return

        api_key = (os.environ.get(llm.api_key_env) or "").strip()
        if not api_key:
            raise JauntConfigError(
                f"Missing API key: {llm.api_key_env}. "
                f"Set it in the environment or add it to <project_root>/.env."
            )

        if llm.provider == "anthropic":
            try:
                from anthropic import AsyncAnthropic
            except ImportError as e:
                raise JauntConfigError(
                    "The 'anthropic' package is required for provider='anthropic'. "
                    "Install it with: pip install jaunt[anthropic]"
                ) from e
            self._client: Any = AsyncAnthropic(api_key=api_key)
        else:
            try:
                from openai import AsyncOpenAI
            except ImportError as e:
                raise JauntConfigError(
                    "The 'openai' package is required for provider='openai'. "
                    "Install it with: pip install jaunt[openai]"
                ) from e

            kwargs: dict[str, Any] = {"api_key": api_key}
            if llm.provider == "cerebras":
                kwargs["base_url"] = "https://api.cerebras.ai/v1"
            self._client = AsyncOpenAI(**kwargs)

    async def _call_llm(self, system: str, user: str) -> str:
        if self._provider == "anthropic":
            resp: Any = await self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            blocks = resp.content
            texts = [b.text for b in blocks if hasattr(b, "text")]
            return "\n".join(texts)

        # OpenAI-compatible (openai, cerebras)
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = resp.choices[0].message.content
        if not isinstance(content, str):
            raise RuntimeError("LLM returned empty content.")
        return content

    async def _run_aider(self, existing_content: str, library_info_block: str) -> str:
        task_body = render_template(
            self._user_prompt,
            {
                "existing_content": existing_content,
                "library_info_block": library_info_block,
            },
        ).strip()
        contract = (
            "# Contract\n\n"
            "Update the target SKILL.md file in place.\n\n"
            "## System\n\n"
            f"{self._system_prompt.strip()}\n\n"
            "## Task\n\n"
            f"{task_body}\n"
        )
        task = AgentTask(
            kind="skill_update",
            mode=self._aider.skill_mode,  # type: ignore[arg-type]
            instruction=(
                "Edit only `workspace/SKILL.md`.\n"
                "Read and follow `context/contract.md` first.\n"
                "Use `context/library_info.md` as read-only reference material.\n"
                "Do not edit files under `context/`.\n"
                "Output the completed Markdown in `workspace/SKILL.md`.\n"
            ),
            target_file=AgentFile(relative_path="workspace/SKILL.md", content=existing_content),
            read_only_files=[
                AgentFile(relative_path="context/contract.md", content=contract),
                AgentFile(relative_path="context/library_info.md", content=library_info_block),
            ],
        )
        result = await self._executor.run_task(task)
        return result.output

    async def build_skill(
        self,
        existing_content: str,
        lib_contents: list[LibContent],
        *,
        max_source_chars: int = 100_000,
        progress: Callable[[str, str], None] | None = None,
    ) -> str:
        """Elaborate/update SKILL.md using gathered library info + LLM."""
        if progress is not None:
            progress("context", "collecting library context")
        # Gather additional source files beyond what inspect_lib collected
        source_sections: list[str] = []
        total_chars = 0

        for lc in lib_contents:
            section = f"## Library: {lc.ref.name}"
            if lc.version:
                section += f"=={lc.version}"
            section += "\n"

            if lc.summary:
                section += f"\nSummary: {lc.summary}\n"
            if lc.readme:
                section += f"\n### README\n{lc.readme}\n"
            if lc.module_structure:
                section += f"\n### Module structure\n{lc.module_structure}\n"
            if lc.public_api:
                section += f"\n### Public API\n{lc.public_api}\n"
            if lc.extra_context:
                section += f"\n### Additional context\n{lc.extra_context}\n"

            # Gather additional source files
            extra_source = _gather_extra_source(lc, max_chars=max_source_chars - total_chars)
            if extra_source:
                section += f"\n### Source excerpts\n{extra_source}\n"
                total_chars += len(extra_source)

            source_sections.append(section)
            if total_chars >= max_source_chars:
                break

        library_info_block = "\n\n".join(source_sections)
        user_msg = render_template(
            self._user_prompt,
            {
                "existing_content": existing_content,
                "library_info_block": library_info_block,
            },
        )

        last_err: Exception | None = None
        for attempt in range(1, 3):
            try:
                if progress is not None:
                    progress("attempt", f"{attempt}/2")
                if self._agent.engine == "aider":
                    out = await self._run_aider(existing_content, library_info_block)
                else:
                    out = await self._call_llm(self._system_prompt, user_msg)
                stripped = strip_markdown_fences(out)
                if progress is not None:
                    progress("validating", f"attempt {attempt}")
                errs = validate_skill_markdown(stripped)
                if errs:
                    raise RuntimeError("; ".join(errs))
                if progress is not None:
                    progress("done", f"attempt {attempt}")
                return stripped
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt >= 2:
                    break
                if progress is not None:
                    progress("retry", f"attempt {attempt}")
                await asyncio.sleep(0.35 * attempt)

        assert last_err is not None
        raise last_err


def _gather_extra_source(lc: LibContent, *, max_chars: int) -> str:
    """Read additional .py files from library for richer context."""
    if max_chars <= 0:
        return ""

    from jaunt.lib_inspect import _resolve_import_root

    parts: list[str] = []
    total = 0

    for root_name in lc.ref.import_roots:
        resolved = _resolve_import_root(root_name)
        if resolved is None:
            continue
        root_path, is_package = resolved

        if not is_package:
            # Single-file module — read it directly
            try:
                content = root_path.read_text(encoding="utf-8")
                chunk = f"#### {root_path.name}\n```python\n{content}\n```\n"
                parts.append(chunk)
                total += len(chunk)
            except Exception:  # noqa: BLE001
                pass
            continue

        py_files: list[Path] = []
        _collect_source_files(root_path, py_files, max_files=20)

        for f in py_files:
            if total >= max_chars:
                break
            try:
                content = f.read_text(encoding="utf-8")
                rel = str(f.relative_to(root_path))
                chunk = f"#### {rel}\n```python\n{content}\n```\n"
                parts.append(chunk)
                total += len(chunk)
            except Exception:  # noqa: BLE001
                pass

    return "\n".join(parts)


def _collect_source_files(root: Path, out: list[Path], *, max_files: int) -> None:
    """Collect .py files, skip tests/vendored, prioritize __init__.py and short files."""
    skip_dirs = {"tests", "test", "vendor", "vendored", "_vendor", "__pycache__", ".git"}

    candidates: list[tuple[int, Path]] = []
    try:
        for dirpath_str, dirnames, filenames in os.walk(root, topdown=True):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
            for fn in filenames:
                if fn.endswith(".py"):
                    fp = Path(dirpath_str) / fn
                    try:
                        size = fp.stat().st_size
                        # Prioritize __init__.py (size 0) and small files
                        priority = 0 if fn == "__init__.py" else size
                        candidates.append((priority, fp))
                    except OSError:
                        pass
    except OSError:
        pass

    candidates.sort(key=lambda t: t[0])
    out.extend(fp for _, fp in candidates[:max_files])
