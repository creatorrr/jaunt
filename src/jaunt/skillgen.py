from __future__ import annotations

from jaunt.agent_runtime import AgentFile, AgentTask
from jaunt.codex_executor import CodexExecutor
from jaunt.config import AgentConfig, CodexConfig, LLMConfig
from jaunt.generate.shared import load_prompt, render_template
from jaunt.skill_agent import strip_markdown_fences, validate_skill_markdown


class CodexSkillGenerator:
    def __init__(self, llm: LLMConfig, agent: AgentConfig, codex: CodexConfig) -> None:
        self._executor = CodexExecutor(codex, llm)
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
            mode="code",
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
