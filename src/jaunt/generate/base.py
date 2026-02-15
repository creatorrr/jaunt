from __future__ import annotations

from abc import ABC, abstractmethod
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


@dataclass(frozen=True, slots=True)
class GenerationResult:
    attempts: int
    source: str | None
    errors: list[str]


class GeneratorBackend(ABC):
    @property
    def supports_structured_output(self) -> bool:
        """Whether this backend uses provider-native structured output."""
        return False

    @abstractmethod
    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> str:
        """Generate a Python module for the given context (returns source code)."""

    async def generate_with_retry(
        self, ctx: ModuleSpecContext, *, max_attempts: int = 2
    ) -> GenerationResult:
        """Generate code, validate, and retry with error context (deterministic)."""

        attempts = 0
        last_source: str | None = None
        last_errors: list[str] = []
        extra_ctx: list[str] | None = None

        while attempts < max_attempts:
            attempts += 1
            last_source = await self.generate_module(ctx, extra_error_context=extra_ctx)
            last_errors = validate_generated_source(last_source, ctx.expected_names)
            if not last_errors:
                return GenerationResult(attempts=attempts, source=last_source, errors=[])

            if attempts >= max_attempts:
                break

            # Retry with appended context describing what was wrong previously.
            retry_ctx = [f"previous output errors: {e}" for e in last_errors]
            extra_ctx = (extra_ctx or []) + retry_ctx

        return GenerationResult(attempts=attempts, source=last_source, errors=last_errors)
