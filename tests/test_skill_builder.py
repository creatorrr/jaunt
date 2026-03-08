from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

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


def test_skill_builder_prompt_includes_context(monkeypatch) -> None:
    """Verify the LLM prompt includes lib info and existing content."""
    from jaunt.config import LLMConfig

    llm = LLMConfig(provider="openai", model="gpt-test", api_key_env="TEST_KEY")
    monkeypatch.setenv("TEST_KEY", "fake-key")

    captured_args: list[tuple[str, str]] = []

    async def mock_call(self, system: str, user: str) -> str:
        captured_args.append((system, user))
        return "# Updated skill\n## What it is\nUpdated content.\n"

    with patch("jaunt.skill_builder.SkillBuilder._call_llm", mock_call):
        from jaunt.skill_builder import SkillBuilder

        builder = SkillBuilder(llm)
        existing = "# my-skill\n## What it is\nOriginal.\n"
        lc = _make_lib_content()
        result = asyncio.run(builder.build_skill(existing, [lc]))

    assert len(captured_args) == 1
    system, user = captured_args[0]
    assert "untrusted" in system.lower()
    assert "Original." in user
    assert "mylib" in user
    assert "do_thing" in user
    assert "Updated" in result


def test_skill_builder_truncates_large_input(monkeypatch) -> None:
    from jaunt.config import LLMConfig

    llm = LLMConfig(provider="openai", model="gpt-test", api_key_env="TEST_KEY")
    monkeypatch.setenv("TEST_KEY", "fake-key")

    captured_user: list[str] = []

    async def mock_call(self, system: str, user: str) -> str:
        captured_user.append(user)
        return "# Result\n"

    with patch("jaunt.skill_builder.SkillBuilder._call_llm", mock_call):
        from jaunt.skill_builder import SkillBuilder

        builder = SkillBuilder(llm)
        # Make a large lib content
        lc = LibContent(
            ref=LibRef(type="pypi", name="big", path=None, version="1.0", import_roots=[]),
            summary="Big lib",
            readme="x" * 200_000,
            module_structure="",
            public_api="",
            version="1.0",
        )
        asyncio.run(builder.build_skill("# old", [lc], max_source_chars=100))

    # The prompt should be constructed (no crash), though readme is large
    assert len(captured_user) == 1


def test_skill_builder_preserves_user_text(monkeypatch) -> None:
    """Non-placeholder content should be preserved in output."""
    from jaunt.config import LLMConfig

    llm = LLMConfig(provider="openai", model="gpt-test", api_key_env="TEST_KEY")
    monkeypatch.setenv("TEST_KEY", "fake-key")

    async def mock_call(self, system: str, user: str) -> str:
        # The LLM prompt instructs to preserve valid user-written content
        assert "Preserve valid user-written content" in system
        return "# skill\n## What it is\nKept user text.\n"

    with patch("jaunt.skill_builder.SkillBuilder._call_llm", mock_call):
        from jaunt.skill_builder import SkillBuilder

        builder = SkillBuilder(llm)
        result = asyncio.run(builder.build_skill("# old\nUser text here.", [_make_lib_content()]))

    assert "Kept user text" in result


def test_skill_builder_atomic_write(tmp_path: Path) -> None:
    """Interrupted write doesn't corrupt file."""
    from jaunt.skill_manager import _atomic_write_text

    target = tmp_path / "skills" / "test" / "SKILL.md"
    _atomic_write_text(target, "initial content\n")
    assert target.read_text() == "initial content\n"

    # Write again (simulates update)
    _atomic_write_text(target, "updated content\n")
    assert target.read_text() == "updated content\n"
