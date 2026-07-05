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
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import cast

from jaunt.config import CodexConfig, LLMConfig, PromptsConfig
from jaunt.errors import JauntGenerationError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext, TokenUsage
from jaunt.generate.shared import load_prompt
from jaunt.skill_seed import seed_skills_into_workspace


ADVISORIES_INSTRUCTION = (
    "After the code is complete, end your FINAL message with a line `ADVISORIES:` "
    "followed by one line per logical issue you noticed while implementing: spec "
    "ambiguities, contradictions between a spec and a dependency's documented API, "
    "or suspected bugs in dependency code you read. Write `ADVISORIES: none` if "
    "there is nothing to report. Do not list routine implementation choices."
)
_ADVISORY_HEADING_RE = re.compile(r"^\s*#{0,6}\s*ADVISORIES\s*:?\s*(?P<rest>.*)$")


def parse_advisories(final_message: str) -> tuple[str, ...]:
    lines = (final_message or "").splitlines()
    start = None
    inline_rest = ""
    for i, line in enumerate(lines):
        m = _ADVISORY_HEADING_RE.match(line)
        if m:
            start, inline_rest = i, (m.group("rest") or "").strip()
    if start is None:
        return ()
    items: list[str] = []
    if inline_rest and inline_rest.lower() != "none":
        items.append(inline_rest)
    for line in lines[start + 1 :]:
        text = line.strip().lstrip("-*").strip()
        if not text:
            continue
        if text.lower() == "none":
            continue
        items.append(text)
    return tuple(items)


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
    ignore_user_config: bool = False,
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
    ]
    if ignore_user_config:
        # Hermetic run: skip ~/.codex/config.toml so user MCP servers / web_search
        # tools are not attached (faster, and avoids tools small models reject).
        args.append("--ignore-user-config")
    args += [
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


def _is_model_config_error(message: str) -> bool:
    """Return whether *message* looks like a model-level config rejection."""

    lower = message.casefold()
    signals = (
        "verbosity",
        "unsupported parameter",
        "unsupported value",
        "not supported",
        "unknown parameter",
        "invalid value for",
        "unexpected parameter",
        "does not support",
    )
    return any(signal in lower for signal in signals)


def _offending_config_key(message: str, keys: Iterable[str]) -> str | None:
    """Return the first config key whose full name or final segment appears in *message*."""

    lower = message.casefold()
    for key in keys:
        folded = key.casefold()
        last_segment = folded.rsplit(".", 1)[-1]
        if folded in lower or last_segment in lower:
            return key
    return None


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
    ) -> tuple[str, TokenUsage | None, tuple[str, ...]]:
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

            for name, content in getattr(ctx, "relevant_context_files", ()) or ():
                (ctx_dir / name).write_text(content, encoding="utf-8")

            # Best-effort: seed builtin + project skills into the workspace so
            # `codex exec` can discover them under `.agents/skills/`. Warnings are
            # non-fatal and intentionally not raised.
            seed_skills_into_workspace(
                root,
                project_root=getattr(ctx, "project_root", None),
                builtin_names=list(getattr(ctx, "builtin_skill_names", ()) or ()),
            )

            prompt = self._build_prompt(ctx, target.relative_to(root), extra_error_context)
            extra_config = dict(self._codex.config or {})
            try:
                result = await run_codex_exec(
                    prompt=prompt,
                    cwd=str(root),
                    sandbox=self._codex.sandbox,
                    model=self._model,
                    reasoning_effort=self._codex.reasoning_effort,
                    extra_config=extra_config,
                )
            except JauntGenerationError as exc:
                message = str(exc)
                if not extra_config or not _is_model_config_error(message):
                    raise
                offending_key = _offending_config_key(message, extra_config.keys())
                retry_config = dict(extra_config)
                if offending_key is None:
                    retry_config.clear()
                else:
                    retry_config.pop(offending_key, None)
                result = await run_codex_exec(
                    prompt=prompt,
                    cwd=str(root),
                    sandbox=self._codex.sandbox,
                    model=self._model,
                    reasoning_effort=self._codex.reasoning_effort,
                    extra_config=retry_config,
                )
            source = target.read_text(encoding="utf-8")
            usage = self._usage_from(result)
            advisories = parse_advisories(result.final_message) if result is not None else ()
            return source, usage, advisories

    def _build_prompt(
        self,
        ctx: ModuleSpecContext,
        target_rel: Path,
        extra_error_context: list[str] | None,
    ) -> str:
        preamble = load_prompt(
            "codex_preamble.md",
            self._prompts.build_preamble if self._prompts is not None else None,
        )
        blocks = [preamble.strip()]
        blocks += [
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
            getattr(ctx, "project_overview_block", "") or "",
            getattr(ctx, "build_instructions_block", "") or "",
            getattr(ctx, "module_contract_block", "") or "",
            getattr(ctx, "base_contract_block", "") or "",
            getattr(ctx, "package_context_block", "") or "",
            getattr(ctx, "repo_map_block", "") or "",
        ]
        blocks.append(
            "Relevant library and tooling skills are available in `.agents/skills/`. "
            "Consult them when they apply."
        )
        blocks.append(
            "Edit ONLY the target file. Do not create other files, run tests, or modify "
            "anything else. Output the full module - no placeholders."
        )
        blocks.append(ADVISORIES_INSTRUCTION)
        # This is the load-bearing prompt path; prompts/*.md are not rendered by Codex.
        if ctx.kind == "test":
            blocks.append(
                "Tester role:\n"
                "- The Implementer sees only redacted pass/fail, so your suite is the sole "
                "gate - make derived cases adversarial, not mirrors of the examples.\n"
                "- Derive every expected value from the contract (the spec docstrings), "
                "NEVER from observed implementation behavior; precommit the expected value "
                "into the assertion.\n"
                "- Tag every test with a tier marker: "
                '@pytest.mark.jaunt_tier("example") for docstring canonical examples, '
                'otherwise @pytest.mark.jaunt_tier("derived") for derived cases.\n'
                "- Name derived cases opaquely, e.g. test_derived_01, NOT "
                "test_empty_list_returns_zero, so the name leaks nothing."
            )
        else:
            blocks.append(
                "Implementer role:\n"
                "- A separate Tester writes the tests; you will never see them.\n"
                "- On repair, derived-tier failures arrive as {case-id, exception-class} "
                "with no expected values, by design.\n"
                "- Do not probe or pattern-match to the hidden cases.\n"
                "- When example checks pass but derived checks fail, re-read the contract "
                "for the general rule, not the specific failing case.\n"
                "- Rationale: this is a closed-book exam graded by an independent examiner."
            )
        relevant = getattr(ctx, "relevant_context_block", "") or ""
        if relevant.strip():
            blocks.append(relevant)
        if extra_error_context:
            blocks.append("Previous attempt problems:\n" + "\n".join(extra_error_context))
        return "\n\n".join(b for b in blocks if b)

    async def complete_text(self, *, system: str, user: str) -> str:
        text, _usage = await self.complete_text_with_usage(system=system, user=user)
        return text

    async def complete_text_with_usage(
        self, *, system: str, user: str
    ) -> tuple[str, TokenUsage | None]:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = "\n\n".join(
                [
                    system.strip(),
                    user.strip(),
                    "Return ONLY the requested text. Do not run any commands or edit any files.",
                ]
            )
            result = await run_codex_exec(
                prompt=prompt,
                cwd=tmp,
                sandbox="read-only",
                model=self._model,
                reasoning_effort=self._codex.reasoning_effort,
            )
            return result.final_message, self._usage_from(result)

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
