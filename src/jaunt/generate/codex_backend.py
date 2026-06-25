"""Codex exec-backed generation backend.

This backend drives the `codex exec` subprocess (one process per call). Each
call is its own subprocess, so it is naturally task-local: there is no
long-lived MCP session/pool to leak across asyncio tasks, no custom MCP
notifications to decode, and -- critically -- with `--skip-git-repo-check`
Codex will write files inside the non-git temp workspace we seed for it.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import cast

from jaunt.config import CodexConfig, LLMConfig, PromptsConfig
from jaunt.errors import JauntGenerationError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext, TokenUsage


class CodexExecResult:
    """Parsed result of a single `codex exec --json` run."""

    __slots__ = (
        "returncode",
        "final_message",
        "usage_input",
        "usage_output",
        "usage_cached",
        "stderr",
    )

    def __init__(
        self,
        *,
        returncode: int,
        final_message: str,
        usage_input: int | None,
        usage_output: int | None,
        usage_cached: int | None,
        stderr: str,
    ) -> None:
        self.returncode = returncode
        self.final_message = final_message
        self.usage_input = usage_input
        self.usage_output = usage_output
        self.usage_cached = usage_cached
        self.stderr = stderr


class _ParsedJsonl:
    """Parsed `codex exec --json` stream details."""

    __slots__ = (
        "final_message",
        "usage_input",
        "usage_output",
        "usage_cached",
        "saw_turn_completed",
        "failure_message",
    )

    def __init__(
        self,
        *,
        final_message: str,
        usage_input: int | None,
        usage_output: int | None,
        usage_cached: int | None,
        saw_turn_completed: bool,
        failure_message: str | None,
    ) -> None:
        self.final_message = final_message
        self.usage_input = usage_input
        self.usage_output = usage_output
        self.usage_cached = usage_cached
        self.saw_turn_completed = saw_turn_completed
        self.failure_message = failure_message


async def run_codex_exec(
    *,
    prompt: str,
    cwd: str,
    sandbox: str,
    model: str,
    reasoning_effort: str,
    extra_config: dict[str, object] | None = None,
) -> CodexExecResult:
    """Run `codex exec` once, passing *prompt* on stdin, and parse the JSONL events.

    The approval policy is pinned to ``never`` and ``--skip-git-repo-check`` is
    always passed so Codex will operate (and write) inside the non-git temp
    workspace. We never use the dangerous full-access bypass: Codex is confined
    to the workspace via ``--sandbox``.
    """

    args: list[str] = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "-C",
        cwd,
        "--sandbox",
        sandbox,
        "-c",
        'approval_policy="never"',
        "-m",
        model,
        "-c",
        f"model_reasoning_effort={_toml_value(reasoning_effort)}",
    ]
    for key, value in (extra_config or {}).items():
        args += ["-c", f"{key}={_toml_value(value)}"]
    # Stream events as JSONL so we can parse the final agent message + token usage.
    args += ["--json", "-"]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate(prompt.encode("utf-8"))
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    parsed = _parse_jsonl(stdout)
    returncode = proc.returncode if proc.returncode is not None else -1
    failure_message = parsed.failure_message
    if failure_message is None:
        if returncode != 0:
            failure_message = f"exit code {returncode}"
        elif not parsed.saw_turn_completed:
            failure_message = "no turn.completed event (protocol failure)"
    if failure_message is not None:
        raise JauntGenerationError(_format_exec_failure(failure_message, stderr))

    return CodexExecResult(
        returncode=returncode,
        final_message=parsed.final_message,
        usage_input=parsed.usage_input,
        usage_output=parsed.usage_output,
        usage_cached=parsed.usage_cached,
        stderr=stderr,
    )


def _toml_value(value: object) -> str:
    """Render *value* as a `-c key=value` TOML scalar for `codex exec`."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    # Strings are quoted so codex parses them as TOML strings (not bare idents).
    return json.dumps(str(value))


def _parse_jsonl(stdout: str) -> _ParsedJsonl:
    """Extract the final agent message and token usage from `codex exec --json` output."""

    final_message = ""
    usage_input: int | None = None
    usage_output: int | None = None
    usage_cached: int | None = None
    saw_turn_completed = False
    failure_message: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    final_message = text
        elif etype == "turn.failed":
            if failure_message is None:
                failure_message = f"turn.failed: {_turn_failed_message(event)}"
        elif etype == "error":
            if failure_message is None:
                failure_message = f"error event: {_error_event_message(event)}"
        elif etype == "turn.completed":
            saw_turn_completed = True
            usage = event.get("usage")
            if isinstance(usage, dict):
                pin = usage.get("input_tokens")
                pout = usage.get("output_tokens")
                pcached = usage.get("cached_input_tokens")
                if isinstance(pin, int):
                    usage_input = pin
                if isinstance(pout, int):
                    usage_output = pout
                if isinstance(pcached, int):
                    usage_cached = pcached
    return _ParsedJsonl(
        final_message=final_message,
        usage_input=usage_input,
        usage_output=usage_output,
        usage_cached=usage_cached,
        saw_turn_completed=saw_turn_completed,
        failure_message=failure_message,
    )


def _turn_failed_message(event: dict[str, object]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        error_dict = cast(dict[str, object], error)
        message = error_dict.get("message")
        if isinstance(message, str) and message.strip():
            return message
    elif isinstance(error, str) and error.strip():
        return error
    return _raw_event(event)


def _error_event_message(event: dict[str, object]) -> str:
    message = event.get("message")
    if isinstance(message, str) and message.strip():
        return message
    return _raw_event(event)


def _raw_event(event: dict[str, object]) -> str:
    return json.dumps(event, sort_keys=True, default=str)


def _format_exec_failure(reason: str, stderr: str) -> str:
    message = f"codex exec failed: {reason}"
    clean_stderr = stderr.strip()
    if clean_stderr:
        message += f"\nstderr:\n{_truncate_stderr(clean_stderr)}"
    return message


def _truncate_stderr(stderr: str, *, limit: int = 4000) -> str:
    if len(stderr) <= limit:
        return stderr
    return stderr[:limit] + "\n... [stderr truncated]"


class CodexBackend(GeneratorBackend):
    def __init__(
        self,
        codex: CodexConfig,
        llm: LLMConfig,
        prompts: PromptsConfig | None = None,
    ) -> None:
        self._codex = codex
        self._llm = llm
        self._prompts = prompts
        self._model = codex.model or llm.model

    @property
    def provider_name(self) -> str:
        return "codex"

    @property
    def supports_structured_output(self) -> bool:
        return False

    async def aclose(self) -> None:
        # No long-lived resources; each call is its own subprocess. Kept for
        # lifecycle compatibility with the GeneratorBackend protocol.
        return None

    async def generate_module(
        self,
        ctx: ModuleSpecContext,
        *,
        extra_error_context: list[str] | None = None,
    ) -> tuple[str, TokenUsage | None]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / (ctx.generated_module.replace(".", "/") + ".py")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(getattr(ctx, "seed_target_content", "") or "", encoding="utf-8")

            ctx_dir = root / "_context"
            ctx_dir.mkdir()
            for i, (ref, src) in enumerate(
                sorted(ctx.spec_sources.items(), key=lambda kv: str(kv[0]))
            ):
                (ctx_dir / f"spec_{i}.py").write_text(f"# {ref}\n{src}", encoding="utf-8")
            for i, (ref, api) in enumerate(
                sorted(ctx.dependency_apis.items(), key=lambda kv: str(kv[0]))
            ):
                (ctx_dir / f"dep_{i}.pyi").write_text(f"# {ref}\n{api}", encoding="utf-8")

            contract_block = getattr(ctx, "whole_class_contract_block", "") or ""
            if contract_block.strip():
                (ctx_dir / "whole_class_contract.md").write_text(
                    contract_block.rstrip() + "\n", encoding="utf-8"
                )

            prompt = self._build_prompt(ctx, target.relative_to(root), extra_error_context)
            result = await run_codex_exec(
                prompt=prompt,
                cwd=str(root),
                sandbox=self._codex.sandbox,
                model=self._model,
                reasoning_effort=self._codex.reasoning_effort,
                extra_config=dict(self._codex.config or {}),
            )
            source = target.read_text(encoding="utf-8")
            usage = self._usage_from(result)
            return source, usage

    def _build_prompt(
        self,
        ctx: ModuleSpecContext,
        target_rel: Path,
        extra_error_context: list[str] | None,
    ) -> str:
        blocks = [
            f"Write a complete Python module to `{target_rel}` that exports: "
            f"{', '.join(ctx.expected_names)}.",
            "The spec stubs and their docstrings in `_context/spec_*.py` are the "
            "behavioral contract. Read `_context/dep_*.pyi` for available APIs.",
        ]
        if (getattr(ctx, "whole_class_contract_block", "") or "").strip():
            blocks.append(
                "Read `_context/whole_class_contract.md`: implement every "
                "`# jaunt:implement` method, keep preserved methods verbatim, and design "
                "the public API the docstring implies."
            )
        blocks += [
            getattr(ctx, "build_instructions_block", "") or "",
            getattr(ctx, "module_contract_block", "") or "",
            getattr(ctx, "base_contract_block", "") or "",
            getattr(ctx, "package_context_block", "") or "",
            getattr(ctx, "skills_block", "") or "",
        ]
        blocks.append(
            "Edit ONLY the target file. Do not create other files, run tests, or modify "
            "anything else. Output the full module - no placeholders."
        )
        if extra_error_context:
            blocks.append("Previous attempt problems:\n" + "\n".join(extra_error_context))
        return "\n\n".join(b for b in blocks if b)

    async def complete_text(self, *, system: str, user: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = "\n\n".join(
                [
                    system.strip(),
                    user.strip(),
                    "Return ONLY the requested text. Do not run any commands or edit "
                    "any files.",
                ]
            )
            result = await run_codex_exec(
                prompt=prompt,
                cwd=tmp,
                sandbox="read-only",
                model=self._model,
                reasoning_effort=self._codex.reasoning_effort,
            )
            return result.final_message

    def _usage_from(self, result: CodexExecResult | None) -> TokenUsage | None:
        if result is None:
            return None
        pin = result.usage_input
        pout = result.usage_output
        if isinstance(pin, int) and isinstance(pout, int):
            return TokenUsage(
                prompt_tokens=pin,
                completion_tokens=pout,
                model=self._model,
                provider="codex",
                cached_prompt_tokens=result.usage_cached or 0,
            )
        return None
