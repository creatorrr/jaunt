from __future__ import annotations

import os
import re
from importlib import resources
from pathlib import Path
from typing import Any

from jaunt.config import LLMConfig, PromptsConfig
from jaunt.errors import JauntConfigError
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext


def render_template(text: str, mapping: dict[str, str]) -> str:
    """Very small template renderer: replaces `{{name}}` placeholders."""

    rendered = text
    for key, value in mapping.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(?P<code>.*)\n\s*```\s*$", re.DOTALL)


def _strip_markdown_fences(text: str) -> str:
    m = _FENCE_RE.match(text or "")
    if not m:
        return (text or "").strip()
    return (m.group("code") or "").strip()


def _fmt_kv_block(items: list[tuple[str, str]], *, empty: str = "(none)") -> str:
    if not items:
        return empty
    chunks: list[str] = []
    for key, value in items:
        chunks.append(f"# {key}\n{value.rstrip()}\n")
    return "\n".join(chunks).rstrip() + "\n"


class OpenAIBackend(GeneratorBackend):
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

    def _load_prompt(self, default_name: str, override_path: str | None) -> str:
        if override_path:
            return Path(override_path).read_text(encoding="utf-8")

        # Prompts are packaged under src/jaunt/prompts/** (see pyproject include).
        p = resources.files("jaunt") / "prompts" / default_name
        return p.read_text(encoding="utf-8")

    async def _call_openai(self, messages: list[dict[str, str]]) -> str:
        # Keep this as a small method so tests can patch it without relying on
        # SDK internals (which change frequently).
        resp: Any = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
        )
        content = resp.choices[0].message.content
        if not isinstance(content, str):
            raise RuntimeError("OpenAI returned empty content.")
        return content

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
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> str:
        messages = self._render_messages(ctx, extra_error_context=extra_error_context)
        raw = await self._call_openai(messages)
        return _strip_markdown_fences(raw)
