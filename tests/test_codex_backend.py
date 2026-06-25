from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

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


def _backend() -> CodexBackend:
    return CodexBackend(
        CodexConfig(model="gpt-test"),
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
    )


class _FakeProc:
    """Stand-in for the object returned by asyncio.create_subprocess_exec.

    Records the stdin (the prompt) it is handed into *captured["prompt"]* so
    tests can assert on prompt assembly without reassigning bound methods.
    """

    def __init__(
        self,
        stdout: bytes,
        stderr: bytes = b"",
        returncode: int = 0,
        captured: dict[str, object] | None = None,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._captured = captured

    async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
        if self._captured is not None:
            self._captured["prompt"] = (stdin or b"").decode("utf-8")
        return self._stdout, self._stderr


def _usage_jsonl(final_message: str, *, input_tokens: int, output_tokens: int) -> bytes:
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "abc"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": final_message},
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            }
        ),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _install_fake_exec(monkeypatch, *, on_run, stdout: bytes, returncode: int = 0):
    """Patch asyncio.create_subprocess_exec with a fake that calls on_run(args)."""
    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = list(args)
        captured["stdin"] = kwargs.get("stdin")
        on_run(list(args))
        return _FakeProc(stdout, b"", returncode, captured)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return captured


def _cwd_from_args(args: list[str]) -> Path:
    idx = args.index("-C")
    return Path(args[idx + 1])


def test_generate_module_returns_written_source_and_writes_seed(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()
        seen: dict[str, str] = {}

        def on_run(args: list[str]) -> None:
            root = _cwd_from_args(args)
            target = root / "pkg/__generated__/thing.py"
            seen["seed"] = target.read_text(encoding="utf-8")
            seen["spec"] = (root / "_context/spec_0.py").read_text(encoding="utf-8")
            seen["dep"] = (root / "_context/dep_0.pyi").read_text(encoding="utf-8")
            target.write_text(
                "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
                encoding="utf-8",
            )

        _install_fake_exec(
            monkeypatch,
            on_run=on_run,
            stdout=_usage_jsonl("done", input_tokens=10, output_tokens=5),
        )

        source, usage = await backend.generate_module(
            _ctx(seed_target_content="# previous candidate\n")
        )

        assert seen["seed"] == "# previous candidate\n"
        assert "def alpha(): ..." in seen["spec"]
        assert "def helper() -> str: ..." in seen["dep"]
        assert source == "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
        assert usage == TokenUsage(10, 5, model="gpt-test", provider="codex")

    asyncio.run(run())


def test_generate_module_command_line_flags(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()

        def on_run(args: list[str]) -> None:
            root = _cwd_from_args(args)
            (root / "pkg/__generated__/thing.py").write_text(
                "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
                encoding="utf-8",
            )

        captured = _install_fake_exec(
            monkeypatch,
            on_run=on_run,
            stdout=_usage_jsonl("done", input_tokens=1, output_tokens=1),
        )

        await backend.generate_module(_ctx())

        args = cast(list[str], captured["args"])
        assert args[0] == "codex"
        assert args[1] == "exec"
        assert "--skip-git-repo-check" in args
        # sandbox must be workspace-write for a build (writes the target file)
        si = args.index("--sandbox")
        assert args[si + 1] == "workspace-write"
        assert "-C" in args
        # approval policy pinned to never via -c; dangerous bypass never used
        assert '-c' in args and 'approval_policy="never"' in args
        assert "--dangerously-bypass-approvals-and-sandbox" not in args
        # model + reasoning effort
        mi = args.index("-m")
        assert args[mi + 1] == "gpt-test"
        assert any(a.startswith("model_reasoning_effort=") for a in args)
        # JSON streaming + prompt on stdin (trailing "-")
        assert "--json" in args
        assert args[-1] == "-"
        assert captured["stdin"] == asyncio.subprocess.PIPE

    asyncio.run(run())


def test_generate_module_writes_whole_class_contract_file(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()
        seen: dict[str, object] = {}

        def on_run(args: list[str]) -> None:
            root = _cwd_from_args(args)
            seen["seed"] = (root / "pkg/__generated__/thing.py").read_text(encoding="utf-8")
            seen["contract"] = (root / "_context/whole_class_contract.md").read_text(
                encoding="utf-8"
            )
            (root / "pkg/__generated__/thing.py").write_text(
                "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
                encoding="utf-8",
            )

        _install_fake_exec(
            monkeypatch,
            on_run=on_run,
            stdout=_usage_jsonl("done", input_tokens=1, output_tokens=1),
        )

        await backend.generate_module(
            _ctx(
                seed_target_content="class Stack:\n    ...\n",
                whole_class_contract_block="# contract\nfill Stack.push\n",
            )
        )

        assert seen["seed"] == "class Stack:\n    ...\n"
        assert seen["contract"] == "# contract\nfill Stack.push\n"

    asyncio.run(run())


def test_generate_module_prompt_assembly(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()

        def on_run(args: list[str]) -> None:
            root = _cwd_from_args(args)
            (root / "pkg/__generated__/thing.py").write_text(
                "def alpha():\n    pass\n\ndef beta():\n    pass\n",
                encoding="utf-8",
            )

        captured = _install_fake_exec(
            monkeypatch,
            on_run=on_run,
            stdout=_usage_jsonl("done", input_tokens=1, output_tokens=1),
        )

        await backend.generate_module(
            _ctx(),
            extra_error_context=["missing alpha", "missing beta"],
        )

        prompt = cast(str, captured["prompt"])
        assert "alpha, beta" in prompt
        assert "_context/spec_" in prompt
        assert "Edit ONLY the target file" in prompt
        assert "Previous attempt problems:\nmissing alpha\nmissing beta" in prompt

    asyncio.run(run())


def test_complete_text_returns_final_message_and_uses_read_only(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()

        def on_run(_args: list[str]) -> None:
            return None

        captured = _install_fake_exec(
            monkeypatch,
            on_run=on_run,
            stdout=_usage_jsonl("HELLO", input_tokens=3, output_tokens=2),
        )

        result = await backend.complete_text(system="system", user="user")

        assert result == "HELLO"
        args = cast(list[str], captured["args"])
        si = args.index("--sandbox")
        assert args[si + 1] == "read-only"

    asyncio.run(run())


def test_aclose_is_noop() -> None:
    async def run() -> None:
        backend = _backend()
        await backend.aclose()  # must not raise

    asyncio.run(run())


def test_usage_parsed_from_json_and_absent_usage(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()

        def on_run(args: list[str]) -> None:
            root = _cwd_from_args(args)
            (root / "pkg/__generated__/thing.py").write_text(
                "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
                encoding="utf-8",
            )

        # No turn.completed usage event -> usage is None.
        no_usage = (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "done"},
                }
            )
            + "\n"
        ).encode("utf-8")
        _install_fake_exec(monkeypatch, on_run=on_run, stdout=no_usage)

        _source, usage = await backend.generate_module(_ctx())
        assert usage is None

    asyncio.run(run())
