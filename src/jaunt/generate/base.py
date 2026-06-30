"""In what furnace was thy brain? -- abstract LLM generation backend."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
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
    decorator_apis: dict[SpecRef, str] = field(default_factory=dict)
    project_root: Path | None = None
    builtin_skill_names: tuple[str, ...] = ()
    skills_digest: str = ""
    module_contract_block: str = ""
    base_contract_block: str = ""
    blueprint_source: str = ""
    build_instructions_block: str = ""
    attached_test_specs_block: str = ""
    package_context_block: str = ""
    repo_map_block: str = ""
    relevant_context_block: str = ""
    relevant_context_files: tuple[tuple[str, str], ...] = ()
    project_overview_block: str = ""
    module_context_digest: str = ""
    async_runner: str = "asyncio"
    seed_target_content: str = ""
    whole_class_contract_block: str = ""
    whole_class: bool = False


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token counts from a single LLM generation call."""

    prompt_tokens: int
    completion_tokens: int
    model: str
    provider: str
    cached_prompt_tokens: int = 0


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

    def generation_fingerprint(self, ctx: ModuleSpecContext) -> str:
        """Stable fingerprint for freshness invalidation and cache partitioning."""
        payload = {
            "engine": self.provider_name,
            "kind": ctx.kind,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    @abstractmethod
    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, TokenUsage | None]:
        """Generate a Python module for the given context.

        Returns (source_code, optional_token_usage).
        """

    async def complete_text(self, *, system: str, user: str) -> str:
        """Single-shot text completion for contract derivation.

        Default: unsupported. Providers override this. Used only by `jaunt reconcile`
        when docstring prose is unstructured.
        """
        raise NotImplementedError("Contract derivation via model is not supported on this backend.")

    async def complete_text_with_usage(
        self, *, system: str, user: str
    ) -> tuple[str, TokenUsage | None]:
        """Like `complete_text`, but also surfaces token usage when available.

        Default: delegate to `complete_text` and report no usage. Backends that can
        report token counts (e.g. Codex) override this so callers can charge the call
        against a cost budget.
        """
        return await self.complete_text(system=system, user=user), None

    async def generate_with_retry(
        self,
        ctx: ModuleSpecContext,
        *,
        max_attempts: int = 2,
        extra_validator: Callable[[str], list[str]] | None = None,
        initial_error_context: list[str] | None = None,
        progress: Callable[[str, str], None] | None = None,
    ) -> GenerationResult:
        """Generate code, validate, and retry with error context (deterministic)."""

        attempts = 0
        last_source: str | None = None
        last_errors: list[str] = []
        extra_ctx: list[str] | None = list(initial_error_context) if initial_error_context else None
        total_prompt = 0
        total_completion = 0
        total_cached_prompt = 0

        while attempts < max_attempts:
            attempts += 1
            if progress is not None:
                progress("attempt", f"{attempts}/{max_attempts}")
            last_source, usage = await self.generate_module(ctx, extra_error_context=extra_ctx)
            if usage is not None:
                total_prompt += usage.prompt_tokens
                total_completion += usage.completion_tokens
                total_cached_prompt += usage.cached_prompt_tokens

            last_errors = validate_generated_source(last_source, ctx.expected_names)
            if not last_errors and extra_validator is not None:
                last_errors = extra_validator(last_source)
            if not last_errors:
                if progress is not None:
                    progress("done", f"attempt {attempts}")
                agg = (
                    TokenUsage(
                        total_prompt,
                        total_completion,
                        self.model_name,
                        self.provider_name,
                        cached_prompt_tokens=total_cached_prompt,
                    )
                    if total_prompt or total_completion
                    else None
                )
                return GenerationResult(attempts=attempts, source=last_source, errors=[], usage=agg)

            if attempts >= max_attempts:
                break

            # Retry with appended context describing what was wrong previously.
            if progress is not None:
                progress("retry", f"attempt {attempts}")
            retry_ctx = [f"previous output errors: {e}" for e in last_errors]
            extra_ctx = (extra_ctx or []) + retry_ctx

        agg = (
            TokenUsage(
                total_prompt,
                total_completion,
                self.model_name,
                self.provider_name,
                cached_prompt_tokens=total_cached_prompt,
            )
            if total_prompt or total_completion
            else None
        )
        return GenerationResult(
            attempts=attempts, source=last_source, errors=last_errors, usage=agg
        )
