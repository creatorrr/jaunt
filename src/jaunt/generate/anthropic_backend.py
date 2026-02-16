from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from jaunt.config import LLMConfig, PromptsConfig
from jaunt.errors import JauntConfigError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext, TokenUsage
from jaunt.generate.shared import (
    async_test_info,
    fmt_kv_block,
    load_prompt,
    render_template,
    strip_markdown_fences,
)

logger = logging.getLogger("jaunt.generate.anthropic")

_MAX_API_RETRIES = 4
_BASE_BACKOFF_S = 1.0


def _is_retryable(exc: BaseException) -> bool:
    """Return True for transient Anthropic API errors worth retrying."""
    cls_name = type(exc).__name__
    if cls_name in ("RateLimitError", "APITimeoutError", "APIConnectionError"):
        return True
    if cls_name == "APIStatusError":
        status = getattr(exc, "status_code", 0)
        return int(status) >= 500
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return False


_ANTHROPIC_WRITE_MODULE_TOOL: dict[str, Any] = {
    "name": "write_module",
    "description": "Write the generated Python module source code.",
    "input_schema": {
        "type": "object",
        "properties": {
            "python_source": {
                "type": "string",
                "description": "The complete Python source code for the module.",
            },
            "imports_used": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of imported module names.",
            },
            "notes": {
                "type": "string",
                "description": "Optional generation notes.",
            },
        },
        "required": ["python_source"],
    },
}


class AnthropicBackend(GeneratorBackend):
    """Code generation backend using the Anthropic Messages API."""

    @property
    def supports_structured_output(self) -> bool:
        return True

    def __init__(self, llm: LLMConfig, prompts: PromptsConfig | None = None) -> None:
        api_key = (os.environ.get(llm.api_key_env) or "").strip()
        if not api_key:
            raise JauntConfigError(
                f"Missing API key: {llm.api_key_env}. "
                f"Set it in the environment or add it to <project_root>/.env."
            )
        self._model = llm.model
        self._thinking: dict[str, Any] | None = None
        if llm.anthropic_thinking_budget_tokens is not None:
            self._thinking = {
                "type": "enabled",
                "budget_tokens": llm.anthropic_thinking_budget_tokens,
            }

        try:
            from anthropic import AsyncAnthropic  # type: ignore[import-untyped]
        except ImportError as e:
            raise JauntConfigError(
                "The 'anthropic' package is required for provider='anthropic'. "
                "Install it with: pip install jaunt[anthropic]"
            ) from e

        self._client: Any = AsyncAnthropic(api_key=api_key)

        self._build_system = load_prompt(
            "build_system.md", prompts.build_system if prompts else None
        )
        self._build_module = load_prompt(
            "build_module.md", prompts.build_module if prompts else None
        )
        self._test_system = load_prompt("test_system.md", prompts.test_system if prompts else None)
        self._test_module = load_prompt("test_module.md", prompts.test_module if prompts else None)

    @property
    def provider_name(self) -> str:
        return "anthropic"

    async def _call_anthropic(
        self, system: str, messages: list[dict[str, str]]
    ) -> tuple[str, TokenUsage | None]:
        """Call Anthropic Messages API with retry and exponential backoff."""
        last_exc: BaseException | None = None
        for attempt in range(_MAX_API_RETRIES):
            try:
                request_kwargs: dict[str, Any] = {
                    "model": self._model,
                    "max_tokens": 16384,
                    "system": system,
                    "messages": messages,
                }
                if self._thinking is not None:
                    request_kwargs["thinking"] = self._thinking
                resp: Any = await self._client.messages.create(**request_kwargs)
                content = resp.content
                if not content or not hasattr(content[0], "text"):
                    raise RuntimeError("Anthropic returned empty content.")
                usage = None
                if getattr(resp, "usage", None) is not None:
                    usage = TokenUsage(
                        prompt_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
                        completion_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
                        model=self._model,
                        provider="anthropic",
                    )
                return str(content[0].text), usage
            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc) or attempt >= _MAX_API_RETRIES - 1:
                    raise
                delay = _BASE_BACKOFF_S * (2**attempt)
                logger.warning(
                    "Anthropic API error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    _MAX_API_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        raise last_exc  # type: ignore[misc]

    async def _call_anthropic_structured(
        self, system: str, messages: list[dict[str, str]]
    ) -> tuple[str, TokenUsage | None]:
        """Call Anthropic Messages API with tool_use for structured output."""
        last_exc: BaseException | None = None
        for attempt in range(_MAX_API_RETRIES):
            try:
                request_kwargs: dict[str, Any] = {
                    "model": self._model,
                    "max_tokens": 16384,
                    "system": system,
                    "messages": messages,
                    "tools": [_ANTHROPIC_WRITE_MODULE_TOOL],
                    "tool_choice": {"type": "tool", "name": "write_module"},
                }
                if self._thinking is not None:
                    request_kwargs["thinking"] = self._thinking
                resp: Any = await self._client.messages.create(**request_kwargs)
                usage = None
                if getattr(resp, "usage", None) is not None:
                    usage = TokenUsage(
                        prompt_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
                        completion_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
                        model=self._model,
                        provider="anthropic",
                    )
                for block in resp.content:
                    if getattr(block, "type", None) == "tool_use" and block.name == "write_module":
                        source = block.input["python_source"]
                        imports = block.input.get("imports_used", [])
                        notes = block.input.get("notes", "")
                        if imports:
                            logger.debug("Structured output imports_used: %s", imports)
                        if notes:
                            logger.debug("Structured output notes: %s", notes)
                        return source, usage
                raise RuntimeError(
                    "Anthropic response did not contain a write_module tool_use block."
                )
            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc) or attempt >= _MAX_API_RETRIES - 1:
                    raise
                delay = _BASE_BACKOFF_S * (2**attempt)
                logger.warning(
                    "Anthropic API error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    _MAX_API_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        raise last_exc  # type: ignore[misc]

    def _render_messages(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None
    ) -> tuple[str, list[dict[str, str]]]:
        """Return (system_prompt, messages) for the Anthropic Messages API."""
        expected = ", ".join(ctx.expected_names)

        spec_items: list[tuple[str, str]] = []
        for ref, source in sorted(ctx.spec_sources.items(), key=lambda kv: str(kv[0])):
            label = str(ref)
            prompt = ctx.decorator_prompts.get(ref)
            if prompt:
                source = f"{source.rstrip()}\n\n# Decorator prompt\n{prompt.rstrip()}\n"
            spec_items.append((label, source))

        deps_api_items: list[tuple[str, str]] = []
        for ref, api in sorted(ctx.dependency_apis.items(), key=lambda kv: str(kv[0])):
            deps_api_items.append((str(ref), api))

        deps_gen_items: list[tuple[str, str]] = []
        for mod, src in sorted(ctx.dependency_generated_modules.items(), key=lambda kv: kv[0]):
            deps_gen_items.append((mod, src))

        err_items: list[tuple[str, str]] = []
        if extra_error_context:
            for i, line in enumerate(extra_error_context, start=1):
                err_items.append((f"error_context[{i}]", line))

        mapping = {
            "spec_module": ctx.spec_module,
            "generated_module": ctx.generated_module,
            "expected_names": expected,
            "specs_block": fmt_kv_block(spec_items),
            "deps_api_block": fmt_kv_block(deps_api_items),
            "deps_generated_block": fmt_kv_block(deps_gen_items),
            "error_context_block": fmt_kv_block(err_items),
            "async_test_info": async_test_info(ctx.async_runner),
        }

        if ctx.kind == "build":
            system_t = self._build_system
            user_t = self._build_module
        else:
            system_t = self._test_system
            user_t = self._test_module

        system = render_template(system_t, mapping).strip() + "\n"
        user = render_template(user_t, mapping).strip() + "\n"

        messages: list[dict[str, str]] = []
        if (ctx.skills_block or "").strip():
            skills_msg = "External library skills (reference):\n" + ctx.skills_block.strip() + "\n"
            messages.append({"role": "user", "content": skills_msg})
            messages.append(
                {"role": "assistant", "content": "Understood. I'll reference these libraries."}
            )
        messages.append({"role": "user", "content": user})
        return system, messages

    async def generate_module(
        self,
        ctx: ModuleSpecContext,
        *,
        extra_error_context: list[str] | None = None,
    ) -> tuple[str, TokenUsage | None]:
        system, messages = self._render_messages(ctx, extra_error_context=extra_error_context)
        if self.supports_structured_output:
            return await self._call_anthropic_structured(system, messages)
        raw, usage = await self._call_anthropic(system, messages)
        return strip_markdown_fences(raw), usage
