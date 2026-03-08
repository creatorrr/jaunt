from __future__ import annotations

import asyncio
import os
from typing import Any

from jaunt.agent_runtime import AgentFile, AgentTask
from jaunt.aider_executor import AiderExecutor
from jaunt.config import AgentConfig, AiderConfig, LLMConfig
from jaunt.errors import JauntConfigError
from jaunt.generate.shared import load_prompt, render_template
from jaunt.skill_agent import strip_markdown_fences, validate_skill_markdown


class OpenAISkillGenerator:
    """Small OpenAI-backed generator that produces SKILL.md text from a PyPI README."""

    def __init__(self, llm: LLMConfig) -> None:
        api_key = (os.environ.get(llm.api_key_env) or "").strip()
        if not api_key:
            raise JauntConfigError(
                f"Missing API key: {llm.api_key_env}. "
                f"Set it in the environment or add it to <project_root>/.env."
            )

        self._model = llm.model

        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise JauntConfigError(
                "The 'openai' package is required for provider='openai'. "
                "Install it with: pip install jaunt[openai]"
            ) from e

        self._client: Any = AsyncOpenAI(api_key=api_key)
        self._system_prompt = load_prompt("pypi_skill_system.md", None)
        self._user_prompt = load_prompt("pypi_skill_user.md", None)

    async def _call_openai(self, messages: list[dict[str, str]]) -> str:
        resp: Any = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
        )
        content = resp.choices[0].message.content
        if not isinstance(content, str):
            raise RuntimeError("OpenAI returned empty content.")
        return content

    async def generate_skill_markdown(
        self,
        dist: str,
        version: str,
        readme: str,
        readme_type: str,
        *,
        max_readme_chars: int = 50_000,
    ) -> str:
        """Generate skill markdown for a given dist+version from its README text."""

        dist = (dist or "").strip()
        version = (version or "").strip()

        raw = readme or ""
        truncated = False
        if len(raw) > int(max_readme_chars):
            raw = raw[: int(max_readme_chars)]
            truncated = True

        if truncated:
            raw = raw.rstrip() + "\n\n[TRUNCATED]\n"

        system = self._system_prompt.strip()
        user = render_template(
            self._user_prompt,
            {
                "dist": dist,
                "version": version,
                "readme_type": readme_type,
                "readme": raw,
            },
        ).strip()

        messages = [
            {"role": "system", "content": system + "\n"},
            {"role": "user", "content": user + "\n"},
        ]

        last_err: Exception | None = None
        for attempt in range(1, 3):
            try:
                out = await self._call_openai(messages)
                stripped = strip_markdown_fences(out)
                errs = validate_skill_markdown(stripped)
                if errs:
                    raise RuntimeError("; ".join(errs))
                return stripped
            except Exception as e:  # noqa: BLE001 - best-effort retry
                last_err = e
                if attempt >= 2:
                    break
                await asyncio.sleep(0.35 * attempt)

        assert last_err is not None
        raise last_err


class AiderSkillGenerator:
    def __init__(self, llm: LLMConfig, agent: AgentConfig, aider: AiderConfig) -> None:
        self._executor = AiderExecutor(llm, aider)
        self._aider = aider
        self._system_prompt = load_prompt("pypi_skill_system.md", None)
        self._user_prompt = load_prompt("pypi_skill_user.md", None)

    async def generate_skill_markdown(
        self,
        dist: str,
        version: str,
        readme: str,
        readme_type: str,
        *,
        max_readme_chars: int = 50_000,
    ) -> str:
        raw = readme or ""
        truncated = False
        if len(raw) > int(max_readme_chars):
            raw = raw[: int(max_readme_chars)]
            truncated = True
        if truncated:
            raw = raw.rstrip() + "\n\n[TRUNCATED]\n"

        user = render_template(
            self._user_prompt,
            {
                "dist": (dist or "").strip(),
                "version": (version or "").strip(),
                "readme_type": readme_type,
                "readme": raw,
            },
        )
        contract = (
            "# Contract\n\n"
            "Generate the target SKILL.md file in place.\n\n"
            "## System\n\n"
            f"{self._system_prompt.strip()}\n\n"
            "## Task\n\n"
            f"{user.strip()}\n"
        )
        task = AgentTask(
            kind="pypi_skill_generate",
            mode=self._aider.skill_mode,  # type: ignore[arg-type]
            instruction=(
                "Edit only `workspace/SKILL.md`.\n"
                "Read and follow `context/contract.md` first.\n"
                "Use `context/readme.md` as read-only reference material.\n"
                "Do not edit files under `context/`.\n"
                "Output the completed Markdown in `workspace/SKILL.md`.\n"
            ),
            target_file=AgentFile(relative_path="workspace/SKILL.md", content=""),
            read_only_files=[
                AgentFile(relative_path="context/contract.md", content=contract),
                AgentFile(relative_path="context/readme.md", content=raw),
            ],
        )
        result = await self._executor.run_task(task)
        stripped = strip_markdown_fences(result.output)
        errs = validate_skill_markdown(stripped)
        if errs:
            raise RuntimeError("; ".join(errs))
        return stripped
