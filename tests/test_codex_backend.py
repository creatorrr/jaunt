from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from jaunt.config import CodexConfig, LLMConfig
from jaunt.generate.base import TokenUsage
from jaunt.generate.codex_backend import CodexBackend


def _ctx(**overrides):
    values = {
        "kind": "build",
        "generated_module": "pkg.__generated__.thing",
        "expected_names": ["alpha", "beta"],
        "spec_sources": {"pkg.specs:alpha": "def alpha(): ...\n"},
        "dependency_apis": {"pkg.deps:helper": "def helper() -> str: ...\n"},
        "build_instructions_block": "",
        "module_contract_block": "",
        "base_contract_block": "",
        "package_context_block": "",
        "skills_block": "",
        "seed_target_content": "",
        "whole_class_contract_block": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _backend(pool_size: int = 1) -> CodexBackend:
    return CodexBackend(
        CodexConfig(),
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        pool_size=pool_size,
    )


def test_generate_module_returns_written_source_and_writes_seed(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()
        seen: dict[str, str] = {}
        session = SimpleNamespace()

        async def call_tool(name, args):
            target = Path(args["cwd"]) / "pkg/__generated__/thing.py"
            seen["tool"] = name
            seen["seed"] = target.read_text(encoding="utf-8")
            target.write_text(
                "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
                encoding="utf-8",
            )
            return SimpleNamespace(
                structuredContent={"usage": {"input_tokens": 10, "output_tokens": 5}}
            )

        session.call_tool = AsyncMock(side_effect=call_tool)
        monkeypatch.setattr(backend, "_spawn_slot", AsyncMock(return_value=session))

        source, usage = await backend.generate_module(
            _ctx(seed_target_content="# previous candidate\n")
        )

        assert seen == {"tool": "codex", "seed": "# previous candidate\n"}
        assert source == "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
        assert usage == TokenUsage(10, 5, model="gpt-test", provider="codex")

    asyncio.run(run())


def test_generate_module_writes_whole_class_contract_file(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()
        seen: dict[str, object] = {}
        session = SimpleNamespace()

        async def call_tool(name, args):
            root = Path(args["cwd"])
            seen["seed"] = (root / "pkg/__generated__/thing.py").read_text(encoding="utf-8")
            seen["contract"] = (root / "_context/whole_class_contract.md").read_text(
                encoding="utf-8"
            )
            seen["prompt"] = args["prompt"]
            (root / "pkg/__generated__/thing.py").write_text(
                "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
                encoding="utf-8",
            )
            return SimpleNamespace(structuredContent={})

        session.call_tool = AsyncMock(side_effect=call_tool)
        monkeypatch.setattr(backend, "_spawn_slot", AsyncMock(return_value=session))

        await backend.generate_module(
            _ctx(
                seed_target_content="class Stack:\n    ...\n",
                whole_class_contract_block="# contract\nfill Stack.push\n",
            )
        )

        assert seen["seed"] == "class Stack:\n    ...\n"
        assert seen["contract"] == "# contract\nfill Stack.push\n"
        prompt = seen["prompt"]
        assert isinstance(prompt, str)
        assert "_context/whole_class_contract.md" in prompt

    asyncio.run(run())


def test_generate_module_prompt_assembly(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()
        seen: dict[str, object] = {}
        session = SimpleNamespace()

        async def call_tool(name, args):
            seen["args"] = args
            target = Path(args["cwd"]) / "pkg/__generated__/thing.py"
            target.write_text(
                "def alpha():\n    pass\n\ndef beta():\n    pass\n",
                encoding="utf-8",
            )
            return SimpleNamespace(structuredContent={})

        session.call_tool = AsyncMock(side_effect=call_tool)
        monkeypatch.setattr(backend, "_spawn_slot", AsyncMock(return_value=session))

        await backend.generate_module(
            _ctx(),
            extra_error_context=["missing alpha", "missing beta"],
        )

        args = cast(dict[str, object], seen["args"])
        prompt = args["prompt"]
        assert isinstance(prompt, str)
        assert "alpha, beta" in prompt
        assert "_context/spec_" in prompt
        assert "Edit ONLY the target file" in prompt
        assert "Previous attempt problems:\nmissing alpha\nmissing beta" in prompt

    asyncio.run(run())


def test_complete_text_returns_structured_final_message_and_uses_read_only(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()
        seen: dict[str, object] = {}
        session = SimpleNamespace()

        async def call_tool(name, args):
            seen["name"] = name
            seen["args"] = args
            return SimpleNamespace(structuredContent={"lastAgentMessage": "HELLO"})

        session.call_tool = AsyncMock(side_effect=call_tool)
        monkeypatch.setattr(backend, "_spawn_slot", AsyncMock(return_value=session))

        result = await backend.complete_text(system="system", user="user")

        assert result == "HELLO"
        assert seen["name"] == "codex"
        args = cast(dict[str, object], seen["args"])
        assert args["sandbox"] == "read-only"

    asyncio.run(run())


def test_pool_spawns_up_to_pool_size_and_aclose_resets(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend(pool_size=2)
        spawn_count = 0

        async def spawn_slot():
            nonlocal spawn_count
            spawn_count += 1
            session = SimpleNamespace()

            async def call_tool(name, args):
                await asyncio.sleep(0)
                target = Path(args["cwd"]) / "pkg/__generated__/thing.py"
                target.write_text(
                    f"def alpha():\n    return {spawn_count}\n\ndef beta():\n    return 2\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(structuredContent={})

            session.call_tool = AsyncMock(side_effect=call_tool)
            return session

        monkeypatch.setattr(backend, "_spawn_slot", spawn_slot)

        await asyncio.gather(backend.generate_module(_ctx()), backend.generate_module(_ctx()))

        assert spawn_count == 2
        assert backend._started == 2
        assert backend._pool is not None
        assert backend._pool.qsize() == 2

        fake_stack = SimpleNamespace(aclose=AsyncMock())
        backend._stack = fake_stack
        await backend.aclose()

        fake_stack.aclose.assert_awaited_once()
        assert backend._stack is None
        assert backend._pool is None
        assert backend._started == 0

    asyncio.run(run())


def test_extract_usage_handles_present_and_absent_usage() -> None:
    backend = _backend()

    assert backend._extract_usage(
        SimpleNamespace(structuredContent={"usage": {"input_tokens": 10, "output_tokens": 5}})
    ) == TokenUsage(10, 5, model="gpt-test", provider="codex")
    assert backend._extract_usage(SimpleNamespace(structuredContent=None)) is None
    assert backend._extract_usage(SimpleNamespace()) is None
