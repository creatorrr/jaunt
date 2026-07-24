from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from jaunt.config import CodexConfig, LLMConfig
from jaunt.errors import (
    JauntGenerationError,
    JauntQuotaGenerationError,
    JauntTransientGenerationError,
)
from jaunt.generate.base import TokenUsage
from jaunt.generate.codex_backend import CodexBackend, _is_model_config_error


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
        "skills_digest": "",
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


def _usage_jsonl(
    final_message: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int | None = None,
) -> bytes:
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if cached_input_tokens is not None:
        usage["cached_input_tokens"] = cached_input_tokens
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
                "usage": usage,
            }
        ),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _failed_jsonl(message: str) -> bytes:
    return (
        json.dumps({"type": "turn.started"})
        + "\n"
        + json.dumps({"type": "turn.failed", "error": {"message": message}})
        + "\n"
    ).encode("utf-8")


def _install_fake_exec(monkeypatch, *, on_run, stdout, returncode=0):
    """Patch asyncio.create_subprocess_exec with a fake that calls on_run(args)."""
    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        arg_list = list(args)
        captured["args"] = arg_list
        captured["stdin"] = kwargs.get("stdin")
        on_run(arg_list)
        current_stdout = stdout(arg_list) if callable(stdout) else stdout
        current_returncode = returncode(arg_list) if callable(returncode) else returncode
        return _FakeProc(current_stdout, b"", current_returncode, captured)

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

        source, usage, _advisories = await backend.generate_module(
            _ctx(seed_target_content="# previous candidate\n")
        )

        assert seen["seed"] == "# previous candidate\n"
        assert "def alpha(): ..." in seen["spec"]
        assert "def helper() -> str: ..." in seen["dep"]
        assert source == "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
        assert usage == TokenUsage(10, 5, model="gpt-test", provider="codex")

    asyncio.run(run())


def test_generate_module_seeds_skills(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()
        seen: dict[str, object] = {}

        def on_run(args: list[str]) -> None:
            root = _cwd_from_args(args)
            skills_root = root / ".agents" / "skills"
            seen["skills"] = sorted(p.name for p in skills_root.glob("*"))
            (root / "pkg/__generated__/thing.py").write_text(
                "def alpha():\n    pass\n\ndef beta():\n    pass\n",
                encoding="utf-8",
            )

        captured = _install_fake_exec(
            monkeypatch,
            on_run=on_run,
            stdout=_usage_jsonl("done", input_tokens=1, output_tokens=1),
        )

        await backend.generate_module(_ctx(builtin_skill_names=("ruff", "pytest")))

        prompt = cast(str, captured["prompt"])
        assert seen["skills"] == ["pytest", "ruff"]
        assert "## What it is" not in prompt

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
        assert "-c" in args and 'approval_policy="never"' in args
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


@pytest.mark.asyncio
async def test_run_codex_exec_falls_back_when_cli_lacks_hermetic_flag(
    monkeypatch,
) -> None:
    import jaunt.generate.codex_backend as cb

    calls: list[list[str]] = []

    async def fake_exec(*args, **_kwargs):
        call = list(args)
        calls.append(call)
        if "--ignore-user-config" in call:
            return _FakeProc(
                b"",
                b"error: unexpected argument '--ignore-user-config' found\n",
                2,
            )
        return _FakeProc(
            _usage_jsonl("done", input_tokens=3, output_tokens=1),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await cb.run_codex_exec(
        prompt="work",
        cwd="/tmp",
        sandbox="read-only",
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        ignore_user_config=True,
    )

    assert result.final_message == "done"
    assert len(calls) == 2
    assert "--ignore-user-config" in calls[0]
    assert "--ignore-user-config" not in calls[1]


@pytest.mark.asyncio
async def test_run_codex_exec_classifies_known_capacity_failure_as_transient(
    monkeypatch,
) -> None:
    import jaunt.generate.codex_backend as cb

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc(
            _failed_jsonl("Selected model is at capacity. Please try a different model."),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(JauntTransientGenerationError, match="Selected model is at capacity"):
        await cb.run_codex_exec(
            prompt="work",
            cwd="/tmp",
            sandbox="read-only",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
        )


@pytest.mark.asyncio
async def test_run_codex_exec_classifies_plan_usage_limit_as_quota(monkeypatch) -> None:
    import jaunt.generate.codex_backend as cb

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc(
            _failed_jsonl(
                "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage."
            ),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(JauntQuotaGenerationError, match="hit your usage limit"):
        await cb.run_codex_exec(
            prompt="work",
            cwd="/tmp",
            sandbox="read-only",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
        )


def test_generate_module_retries_once_without_offending_config_key(monkeypatch) -> None:
    async def run() -> None:
        backend = CodexBackend(
            CodexConfig(model="gpt-test", config={"verbosity": "low"}),
            LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        )
        calls: list[list[str]] = []
        failed = _failed_jsonl("Unsupported parameter: verbosity is not supported with this model")
        success = _usage_jsonl("done", input_tokens=10, output_tokens=5)

        def on_run(args: list[str]) -> None:
            calls.append(args)
            if len(calls) == 2:
                root = _cwd_from_args(args)
                (root / "pkg/__generated__/thing.py").write_text(
                    "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
                    encoding="utf-8",
                )

        _install_fake_exec(
            monkeypatch,
            on_run=on_run,
            stdout=lambda _args: failed if len(calls) == 1 else success,
        )

        source, usage, _advisories = await backend.generate_module(_ctx())

        assert len(calls) == 2
        retry_args = calls[1]
        assert not any(arg.startswith("verbosity=") for arg in retry_args)
        assert not any(arg.startswith("model_verbosity=") for arg in retry_args)
        assert source == "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
        assert usage == TokenUsage(10, 5, model="gpt-test", provider="codex")

    asyncio.run(run())


def test_generate_module_config_retry_failure_propagates_without_third_try(monkeypatch) -> None:
    async def run() -> None:
        backend = CodexBackend(
            CodexConfig(model="gpt-test", config={"verbosity": "low"}),
            LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        )
        calls: list[list[str]] = []
        failed = _failed_jsonl("Unsupported parameter: verbosity is not supported with this model")

        def on_run(args: list[str]) -> None:
            calls.append(args)

        _install_fake_exec(monkeypatch, on_run=on_run, stdout=failed)

        with pytest.raises(JauntGenerationError, match="Unsupported parameter"):
            await backend.generate_module(_ctx())

        assert len(calls) == 2

    asyncio.run(run())


def test_generate_module_non_config_failure_does_not_retry(monkeypatch) -> None:
    async def run() -> None:
        backend = CodexBackend(
            CodexConfig(model="gpt-test", config={"verbosity": "low"}),
            LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        )
        calls: list[list[str]] = []

        def on_run(args: list[str]) -> None:
            calls.append(args)

        _install_fake_exec(
            monkeypatch,
            on_run=on_run,
            stdout=_failed_jsonl("model overloaded"),
        )

        with pytest.raises(JauntGenerationError, match="model overloaded"):
            await backend.generate_module(_ctx())

        assert len(calls) == 1

    asyncio.run(run())


def test_generate_module_config_failure_without_extra_config_does_not_retry(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()
        calls: list[list[str]] = []

        def on_run(args: list[str]) -> None:
            calls.append(args)

        _install_fake_exec(
            monkeypatch,
            on_run=on_run,
            stdout=_failed_jsonl(
                "Unsupported parameter: verbosity is not supported with this model"
            ),
        )

        with pytest.raises(JauntGenerationError, match="Unsupported parameter"):
            await backend.generate_module(_ctx())

        assert len(calls) == 1

    asyncio.run(run())


def test_is_model_config_error_classifies_conservatively() -> None:
    assert _is_model_config_error(
        "turn.failed: Unsupported parameter: verbosity is not supported with this model"
    )
    assert not _is_model_config_error("exit code 17")
    assert not _is_model_config_error("model overloaded")
    assert not _is_model_config_error("no turn.completed event (protocol failure)")
    assert not _is_model_config_error("bad request")


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

        # turn.completed without usage -> usage is None.
        no_usage = (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "done"},
                }
            )
            + "\n"
            + json.dumps({"type": "turn.completed"})
            + "\n"
        ).encode("utf-8")
        _install_fake_exec(monkeypatch, on_run=on_run, stdout=no_usage)

        _source, usage, _advisories = await backend.generate_module(_ctx())
        assert usage is None

    asyncio.run(run())


def test_nonzero_exit_raises_generation_error(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()

        def on_run(_args: list[str]) -> None:
            return None

        _install_fake_exec(
            monkeypatch,
            on_run=on_run,
            stdout=_usage_jsonl("done", input_tokens=1, output_tokens=1),
            returncode=17,
        )

        with pytest.raises(JauntGenerationError, match="exit code 17"):
            await backend.generate_module(_ctx())

    asyncio.run(run())


def test_turn_failed_event_raises_generation_error(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()

        def on_run(_args: list[str]) -> None:
            return None

        failed = (
            json.dumps({"type": "turn.started"})
            + "\n"
            + json.dumps({"type": "turn.failed", "error": {"message": "model overloaded"}})
            + "\n"
        ).encode("utf-8")
        _install_fake_exec(monkeypatch, on_run=on_run, stdout=failed)

        with pytest.raises(JauntGenerationError, match="turn.failed: model overloaded"):
            await backend.generate_module(_ctx())

    asyncio.run(run())


def test_top_level_error_event_raises_generation_error(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()

        def on_run(_args: list[str]) -> None:
            return None

        errored = (json.dumps({"type": "error", "message": "bad request"}) + "\n").encode("utf-8")
        _install_fake_exec(monkeypatch, on_run=on_run, stdout=errored)

        with pytest.raises(JauntGenerationError, match="error event: bad request"):
            await backend.generate_module(_ctx())

    asyncio.run(run())


def test_missing_turn_completed_raises_protocol_error(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()

        def on_run(_args: list[str]) -> None:
            return None

        incomplete = (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "done"},
                }
            )
            + "\n"
        ).encode("utf-8")
        _install_fake_exec(monkeypatch, on_run=on_run, stdout=incomplete)

        with pytest.raises(JauntGenerationError, match="no turn.completed event"):
            await backend.generate_module(_ctx())

    asyncio.run(run())


def test_cached_input_tokens_parsed_into_usage(monkeypatch) -> None:
    async def run() -> None:
        backend = _backend()

        def on_run(args: list[str]) -> None:
            root = _cwd_from_args(args)
            (root / "pkg/__generated__/thing.py").write_text(
                "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
                encoding="utf-8",
            )

        _install_fake_exec(
            monkeypatch,
            on_run=on_run,
            stdout=_usage_jsonl(
                "done",
                input_tokens=10,
                output_tokens=5,
                cached_input_tokens=7,
            ),
        )

        _source, usage, _advisories = await backend.generate_module(_ctx())
        assert usage == TokenUsage(
            10,
            5,
            model="gpt-test",
            provider="codex",
            cached_prompt_tokens=7,
        )

    asyncio.run(run())


def test_build_prompt_includes_repo_map_block() -> None:
    backend = _backend()
    ctx = _ctx(repo_map_block="## Repository map\nsrc/a.py — does a")
    prompt = backend._build_prompt(ctx, Path("pkg/__generated__/m.py"), None)
    assert "## Repository map" in prompt
    assert prompt.index("## Repository map") > prompt.index("Write a complete Python module")


def test_generate_writes_relevant_context_files(monkeypatch) -> None:
    # Capture the _context dir contents by stubbing run_codex_exec.
    import jaunt.generate.codex_backend as cb

    written: dict[str, str] = {}

    async def _fake_run(*, prompt, cwd, **kw):
        ctx_dir = Path(cwd) / "_context"
        for p in ctx_dir.glob("relevant_*.py"):
            written[p.name] = p.read_text(encoding="utf-8")
        # Write the target so generate_module can read it back (the single
        # .py outside _context).
        for p in Path(cwd).rglob("*.py"):
            if "_context" not in p.parts:
                p.write_text("x = 1\n", encoding="utf-8")
        return None

    monkeypatch.setattr(cb, "run_codex_exec", _fake_run)
    backend = _backend()
    ctx = _ctx(
        relevant_context_block="Read `_context/relevant_*.py` ...",
        relevant_context_files=(("relevant_0.py", "# src/a.py\ndef f(): ...\n"),),
    )
    asyncio.run(backend.generate_module(ctx))
    assert "relevant_0.py" in written and "def f()" in written["relevant_0.py"]


def test_build_prompt_test_kind_has_tester_section() -> None:
    backend = _backend()
    ctx = _ctx(kind="test")
    prompt = backend._build_prompt(ctx, Path("pkg/__generated__/m.py"), None)
    assert "Tester role:" in prompt
    assert "Implementer role:" not in prompt
    assert "jaunt_tier" in prompt


def test_build_prompt_build_kind_has_implementer_section() -> None:
    backend = _backend()
    ctx = _ctx()
    prompt = backend._build_prompt(ctx, Path("pkg/__generated__/m.py"), None)
    assert "Implementer role:" in prompt
    assert "Tester role:" not in prompt


@pytest.mark.asyncio
async def test_run_codex_exec_terminates_child_when_cancelled(monkeypatch) -> None:
    import jaunt.generate.codex_backend as cb

    started = asyncio.Event()

    class BlockingProcess:
        returncode: int | None = None
        terminated = False
        killed = False

        async def communicate(self, _stdin: bytes) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.returncode = -15
            return self.returncode

    process = BlockingProcess()

    async def fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    task = asyncio.create_task(
        cb.run_codex_exec(
            prompt="work",
            cwd="/tmp",
            sandbox="read-only",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated is True
    assert process.killed is False
