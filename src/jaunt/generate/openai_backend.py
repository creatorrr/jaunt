from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from jaunt.config import LLMConfig, PromptsConfig
from jaunt.errors import JauntConfigError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.generate.shared import (
    fmt_kv_block,
    load_prompt,
    render_template,
    strip_markdown_fences,
)

logger = logging.getLogger("jaunt.generate.openai")

_MAX_API_RETRIES = 4
_BASE_BACKOFF_S = 1.0

# Aliases for backward compatibility (tests import these names directly).
_strip_markdown_fences = strip_markdown_fences
_fmt_kv_block = fmt_kv_block


def _is_retryable(exc: BaseException) -> bool:
    """Return True for transient API errors worth retrying."""
    # openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError,
    # and 5xx APIStatusError are retryable.
    cls_name = type(exc).__name__
    if cls_name in ("RateLimitError", "APITimeoutError", "APIConnectionError"):
        return True
    if cls_name == "APIStatusError":
        status = getattr(exc, "status_code", 0)
        return int(status) >= 500
    # Catch generic connection/timeout errors.
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return False


_OPENAI_MODULE_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "module_output",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "python_source": {"type": "string"},
                "imports_used": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["python_source"],
            "additionalProperties": False,
        },
    },
}


class OpenAIBackend(GeneratorBackend):
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

        # OpenAI SDK is an optional import for the rest of the system; only this
        # backend module imports it.
        from openai import AsyncOpenAI

        self._client: Any = AsyncOpenAI(api_key=api_key)

        build_system_override = prompts.build_system if prompts else None
        build_module_override = prompts.build_module if prompts else None
        test_system_override = prompts.test_system if prompts else None
        test_module_override = prompts.test_module if prompts else None

        self._build_system = self._load_prompt("build_system.md", build_system_override)
        self._build_module = self._load_prompt("build_module.md", build_module_override)
        self._test_system = self._load_prompt("test_system.md", test_system_override)
        self._test_module = self._load_prompt("test_module.md", test_module_override)

    @staticmethod
    def _load_prompt(default_name: str, override_path: str | None) -> str:
        return load_prompt(default_name, override_path)

    async def _call_openai(self, messages: list[dict[str, str]]) -> str:
        """Call OpenAI API with retry and exponential backoff for transient errors."""
        last_exc: BaseException | None = None
        for attempt in range(_MAX_API_RETRIES):
            try:
                resp: Any = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                )
                content = resp.choices[0].message.content
                if not isinstance(content, str):
                    raise RuntimeError("OpenAI returned empty content.")
                return content
            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc) or attempt >= _MAX_API_RETRIES - 1:
                    raise
                delay = _BASE_BACKOFF_S * (2**attempt)
                logger.warning(
                    "OpenAI API error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    _MAX_API_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        raise last_exc  # type: ignore[misc]

    async def _call_openai_structured(self, messages: list[dict[str, str]]) -> str:
        """Call OpenAI API with structured output (json_schema response_format)."""
        last_exc: BaseException | None = None
        for attempt in range(_MAX_API_RETRIES):
            try:
                resp: Any = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    response_format=_OPENAI_MODULE_RESPONSE_FORMAT,
                )
                content = resp.choices[0].message.content
                if not isinstance(content, str):
                    raise RuntimeError("OpenAI returned empty content.")
                parsed = json.loads(content)
                source = parsed["python_source"]
                imports = parsed.get("imports_used", [])
                if imports:
                    logger.debug("Structured output imports_used: %s", imports)
                return source
            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc) or attempt >= _MAX_API_RETRIES - 1:
                    raise
                delay = _BASE_BACKOFF_S * (2**attempt)
                logger.warning(
                    "OpenAI API error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    _MAX_API_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        raise last_exc  # type: ignore[misc]

    def _render_messages(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None
    ) -> list[dict[str, str]]:
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
            "specs_block": _fmt_kv_block(spec_items),
            "deps_api_block": _fmt_kv_block(deps_api_items),
            "deps_generated_block": _fmt_kv_block(deps_gen_items),
            "error_context_block": _fmt_kv_block(err_items),
        }

        if ctx.kind == "build":
            system_t = self._build_system
            user_t = self._build_module
        else:
            system_t = self._test_system
            user_t = self._test_module

        system = render_template(system_t, mapping).strip() + "\n"
        user = render_template(user_t, mapping).strip() + "\n"

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        if (ctx.skills_block or "").strip():
            skills_msg = "External library skills (reference):\n" + ctx.skills_block.strip() + "\n"
            messages.append({"role": "user", "content": skills_msg})
        messages.append({"role": "user", "content": user})
        return messages

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> str:
        messages = self._render_messages(ctx, extra_error_context=extra_error_context)
        if self.supports_structured_output:
            return await self._call_openai_structured(messages)
        raw = await self._call_openai(messages)
        return _strip_markdown_fences(raw)
