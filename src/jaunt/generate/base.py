"""In what furnace was thy brain? -- abstract LLM generation backend."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from jaunt.spec_ref import SpecRef
from jaunt.validation import validate_generated_source


@dataclass(frozen=True, slots=True)
class ModuleSpecContext:
    kind: Literal["build", "test"]
    spec_module: str
    generated_module: str
    expected_names: list[str]
    spec_sources: dict[SpecRef, str]
    decorator_prompts: dict[SpecRef, str]
    dependency_apis: dict[SpecRef, str]
    dependency_generated_modules: dict[str, str]
    skills_block: str = ""
    async_runner: str = "asyncio"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token counts from a single LLM generation call."""

    prompt_tokens: int
    completion_tokens: int
    model: str
    provider: str


@dataclass(frozen=True, slots=True)
class GenerationResult:
    attempts: int
    source: str | None
    errors: list[str]
    usage: TokenUsage | None = None


class GeneratorBackend(ABC):
    @property
    def supports_structured_output(self) -> bool:
        """Whether this backend uses provider-native structured output."""
        return False

    @property
    def model_name(self) -> str:
        return getattr(self, "_model", "")

    @property
    def provider_name(self) -> str:
        return ""

    @abstractmethod
    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, TokenUsage | None]:
        """Generate a Python module for the given context.

        Returns (source_code, optional_token_usage).
        """

    async def generate_with_retry(
        self,
        ctx: ModuleSpecContext,
        *,
        max_attempts: int = 2,
        extra_validator: Callable[[str], list[str]] | None = None,
    ) -> GenerationResult:
        """Generate code, validate, and retry with error context (deterministic)."""

        attempts = 0
        last_source: str | None = None
        last_errors: list[str] = []
        extra_ctx: list[str] | None = None
        total_prompt = 0
        total_completion = 0

        while attempts < max_attempts:
            attempts += 1
            last_source, usage = await self.generate_module(ctx, extra_error_context=extra_ctx)
            if usage is not None:
                total_prompt += usage.prompt_tokens
                total_completion += usage.completion_tokens

            last_errors = validate_generated_source(last_source, ctx.expected_names)
            if not last_errors and extra_validator is not None:
                last_errors = extra_validator(last_source)
            if not last_errors:
                agg = (
                    TokenUsage(total_prompt, total_completion, self.model_name, self.provider_name)
                    if total_prompt or total_completion
                    else None
                )
                return GenerationResult(attempts=attempts, source=last_source, errors=[], usage=agg)

            if attempts >= max_attempts:
                break

            # Retry with appended context describing what was wrong previously.
            retry_ctx = [f"previous output errors: {e}" for e in last_errors]
            extra_ctx = (extra_ctx or []) + retry_ctx

        agg = (
            TokenUsage(total_prompt, total_completion, self.model_name, self.provider_name)
            if total_prompt or total_completion
            else None
        )
        return GenerationResult(
            attempts=attempts, source=last_source, errors=last_errors, usage=agg
        )
