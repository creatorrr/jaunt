from __future__ import annotations

import asyncio
from pathlib import Path

from jaunt.agent_runtime import AgentTask
from jaunt.lib_inspect import LibContent, LibRef


def _make_lib_content(name: str = "mylib", summary: str = "A library") -> LibContent:
    return LibContent(
        ref=LibRef(type="pypi", name=name, path=None, version="1.0.0", import_roots=[name]),
        summary=summary,
        readme="# MyLib\nSome readme content.\n",
        module_structure="mylib/\n  __init__.py\n  core.py\n",
        public_api="def do_thing(x: int) -> str  # Do a thing",
        version="1.0.0",
    )


def test_skill_builder_atomic_write(tmp_path: Path) -> None:
    """Interrupted write doesn't corrupt file."""
    from jaunt.skill_manager import _atomic_write_text

    target = tmp_path / "skills" / "test" / "SKILL.md"
    _atomic_write_text(target, "initial content\n")
    assert target.read_text() == "initial content\n"

    # Write again (simulates update)
    _atomic_write_text(target, "updated content\n")
    assert target.read_text() == "updated content\n"


def test_skill_builder_codex_engine_uses_executor(monkeypatch) -> None:
    from jaunt.config import AgentConfig, CodexConfig, LLMConfig
    from jaunt.skill_builder import SkillBuilder

    monkeypatch.delenv("TEST_KEY", raising=False)

    llm = LLMConfig(provider="openai", model="gpt-test", api_key_env="TEST_KEY")
    builder = SkillBuilder(llm, AgentConfig(engine="codex"), codex=CodexConfig())
    seen: dict[str, AgentTask] = {}

    async def fake_run_task(task):
        seen["task"] = task
        return type(
            "Result",
            (),
            {
                "output": "\n".join(
                    [
                        "# skill",
                        "## What it is",
                        "Updated.",
                        "## Core concepts",
                        "Concepts.",
                        "## Common patterns",
                        "Patterns.",
                        "## Gotchas",
                        "Gotchas.",
                        "## Testing notes",
                        "Testing.",
                    ]
                )
            },
        )()

    monkeypatch.setattr(builder._executor, "run_task", fake_run_task)
    result = asyncio.run(builder.build_skill("# old\n", [_make_lib_content()]))
    assert "Updated." in result
    assert seen["task"].kind == "skill_update"
    assert seen["task"].mode == "code"
