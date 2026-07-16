"""In what furnace was thy brain? -- abstract LLM generation backend."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, TypeAlias, cast

from jaunt.errors import JauntTransientGenerationError
from jaunt.spec_ref import SpecRef
from jaunt.validation import validate_generated_source

CandidateValidator: TypeAlias = Callable[[str], list[str] | Awaitable[list[str]]]


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
class GenerationRequest:
    """Language-neutral request passed to a code-generation backend.

    ``target_path`` and every context-file name are root-relative paths inside the
    backend's disposable workspace. The caller owns prompt construction and semantic
    validation; the backend owns only workspace setup, Codex invocation, and reading
    the requested artifact.
    """

    language: Literal["py", "ts"]
    kind: str
    target_path: str
    context_files: Mapping[str, str]
    prompt: str
    cache_payload: Mapping[str, object]
    validator: CandidateValidator
    project_root: Path | None = None
    seed_target_content: str = ""
    builtin_skill_names: tuple[str, ...] = ()


def generation_request_cache_key(
    request: GenerationRequest,
    *,
    model: str,
    provider: str,
    generation_fingerprint: str = "",
) -> str:
    """Return a language-namespaced deterministic key for a generic request."""

    payload = {
        "language": request.language,
        "kind": request.kind,
        "target_path": request.target_path,
        "context_files": dict(sorted(request.context_files.items())),
        "cache_payload": request.cache_payload,
        "seed_target_content": request.seed_target_content,
        "builtin_skill_names": sorted(request.builtin_skill_names),
        "model": model,
        "provider": provider,
        "generation_fingerprint": generation_fingerprint,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
    advisories: tuple[str, ...] = ()
    attempt_errors: tuple[tuple[str, ...], ...] = ()
    infrastructure_retries: int = 0
    infrastructure_errors: tuple[str, ...] = ()
    infrastructure_exhausted: bool = False


GenerationModuleResult: TypeAlias = (
    tuple[str, TokenUsage | None] | tuple[str, TokenUsage | None, tuple[str, ...]]
)

_MAX_INFRASTRUCTURE_RETRIES = 2


async def _call_with_infrastructure_retry(
    call: Callable[[], Awaitable[GenerationModuleResult]],
    *,
    progress: Callable[[str, str], None] | None,
) -> tuple[GenerationModuleResult | None, tuple[str, ...]]:
    """Retry explicitly transient provider failures without spending candidate attempts."""

    errors: list[str] = []
    while True:
        try:
            return await call(), tuple(errors)
        except JauntTransientGenerationError as exc:
            errors.append(str(exc))
            if len(errors) > _MAX_INFRASTRUCTURE_RETRIES:
                return None, tuple(errors)
            if progress is not None:
                progress(
                    "infrastructure-retry",
                    f"{len(errors)}/{_MAX_INFRASTRUCTURE_RETRIES}: {exc}",
                )
            await asyncio.sleep(float(2 ** (len(errors) - 1)))


def _generation_advisories(gen: GenerationModuleResult) -> tuple[str, ...]:
    if len(gen) != 3:
        return ()
    return cast("tuple[str, TokenUsage | None, tuple[str, ...]]", gen)[2]


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
    ) -> GenerationModuleResult:
        """Generate a Python module for the given context.

        Returns (source_code, optional_token_usage) or
        (source_code, optional_token_usage, advisories).
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

    async def generate_request(
        self,
        request: GenerationRequest,
        *,
        extra_error_context: list[str] | None = None,
    ) -> GenerationModuleResult:
        """Generate the target artifact for a language-neutral request.

        Backends opt into this path explicitly. The legacy ``generate_module`` API
        remains abstract so existing fake backends and Python callers keep the same
        contract.
        """

        del request, extra_error_context
        raise NotImplementedError("This backend does not support generic generation requests.")

    async def generate_request_with_retry(
        self,
        request: GenerationRequest,
        *,
        max_attempts: int = 2,
        initial_error_context: list[str] | None = None,
        progress: Callable[[str, str], None] | None = None,
        usage_callback: Callable[[TokenUsage], None] | None = None,
    ) -> GenerationResult:
        """Generate and validate a generic request, feeding diagnostics to retries."""

        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        attempts = 0
        last_source: str | None = None
        last_errors: list[str] = []
        extra_ctx = list(initial_error_context) if initial_error_context else None
        advisories: tuple[str, ...] = ()
        attempt_errors: list[tuple[str, ...]] = []
        total_prompt = 0
        total_completion = 0
        total_cached_prompt = 0
        attempt_request = request
        infrastructure_errors: list[str] = []

        while attempts < max_attempts:
            attempts += 1
            if progress is not None:
                progress("attempt", f"{attempts}/{max_attempts}")
            generated, transient_errors = await _call_with_infrastructure_retry(
                lambda current_request=attempt_request, current_context=extra_ctx: (
                    self.generate_request(
                        current_request,
                        extra_error_context=current_context,
                    )
                ),
                progress=progress,
            )
            infrastructure_errors.extend(transient_errors)
            if generated is None:
                return GenerationResult(
                    attempts=attempts - 1,
                    source=last_source,
                    errors=[transient_errors[-1]],
                    usage=self._aggregate_usage(
                        total_prompt,
                        total_completion,
                        total_cached_prompt,
                    ),
                    advisories=advisories,
                    attempt_errors=tuple(attempt_errors),
                    infrastructure_retries=max(0, len(infrastructure_errors) - 1),
                    infrastructure_errors=tuple(infrastructure_errors),
                    infrastructure_exhausted=True,
                )
            last_source = generated[0]
            usage = generated[1]
            advisories = _generation_advisories(generated)
            if usage is not None:
                if usage_callback is not None:
                    usage_callback(usage)
                total_prompt += usage.prompt_tokens
                total_completion += usage.completion_tokens
                total_cached_prompt += usage.cached_prompt_tokens

            validation = request.validator(last_source)
            last_errors = await validation if inspect.isawaitable(validation) else validation
            if not last_errors:
                if progress is not None:
                    progress("done", f"attempt {attempts}")
                return GenerationResult(
                    attempts=attempts,
                    source=last_source,
                    errors=[],
                    usage=self._aggregate_usage(
                        total_prompt, total_completion, total_cached_prompt
                    ),
                    advisories=advisories,
                    attempt_errors=tuple(attempt_errors),
                    infrastructure_retries=len(infrastructure_errors),
                    infrastructure_errors=tuple(infrastructure_errors),
                )
            attempt_errors.append(tuple(last_errors))
            if attempts < max_attempts:
                if progress is not None:
                    progress("retry", last_errors[0] if last_errors else f"attempt {attempts}")
                retry_context = [f"previous output errors: {error}" for error in last_errors]
                extra_ctx = [*(extra_ctx or []), *retry_context]
                attempt_request = replace(request, seed_target_content=last_source)

        return GenerationResult(
            attempts=attempts,
            source=last_source,
            errors=last_errors,
            usage=self._aggregate_usage(total_prompt, total_completion, total_cached_prompt),
            advisories=advisories,
            attempt_errors=tuple(attempt_errors),
            infrastructure_retries=len(infrastructure_errors),
            infrastructure_errors=tuple(infrastructure_errors),
        )

    def _aggregate_usage(
        self, prompt_tokens: int, completion_tokens: int, cached_prompt_tokens: int
    ) -> TokenUsage | None:
        if not prompt_tokens and not completion_tokens:
            return None
        return TokenUsage(
            prompt_tokens,
            completion_tokens,
            self.model_name,
            self.provider_name,
            cached_prompt_tokens=cached_prompt_tokens,
        )

    async def generate_with_retry(
        self,
        ctx: ModuleSpecContext,
        *,
        max_attempts: int = 2,
        extra_validator: Callable[[str], list[str]] | None = None,
        initial_error_context: list[str] | None = None,
        progress: Callable[[str, str], None] | None = None,
        usage_callback: Callable[[TokenUsage], None] | None = None,
    ) -> GenerationResult:
        """Generate code, validate, and retry with error context (deterministic)."""

        attempts = 0
        last_source: str | None = None
        last_errors: list[str] = []
        extra_ctx: list[str] | None = list(initial_error_context) if initial_error_context else None
        total_prompt = 0
        total_completion = 0
        total_cached_prompt = 0
        infrastructure_errors: list[str] = []

        while attempts < max_attempts:
            attempts += 1
            if progress is not None:
                progress("attempt", f"{attempts}/{max_attempts}")
            gen, transient_errors = await _call_with_infrastructure_retry(
                lambda current_context=extra_ctx: self.generate_module(
                    ctx,
                    extra_error_context=current_context,
                ),
                progress=progress,
            )
            infrastructure_errors.extend(transient_errors)
            if gen is None:
                return GenerationResult(
                    attempts=attempts - 1,
                    source=last_source,
                    errors=[transient_errors[-1]],
                    usage=(
                        TokenUsage(
                            total_prompt,
                            total_completion,
                            self.model_name,
                            self.provider_name,
                            cached_prompt_tokens=total_cached_prompt,
                        )
                        if total_prompt or total_completion
                        else None
                    ),
                    infrastructure_retries=max(0, len(infrastructure_errors) - 1),
                    infrastructure_errors=tuple(infrastructure_errors),
                    infrastructure_exhausted=True,
                )
            last_source = gen[0]
            usage = gen[1]
            attempt_advisories = _generation_advisories(gen)
            if usage is not None:
                if usage_callback is not None:
                    usage_callback(usage)
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
                return GenerationResult(
                    attempts=attempts,
                    source=last_source,
                    errors=[],
                    usage=agg,
                    advisories=attempt_advisories,
                    infrastructure_retries=len(infrastructure_errors),
                    infrastructure_errors=tuple(infrastructure_errors),
                )

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
            attempts=attempts,
            source=last_source,
            errors=last_errors,
            usage=agg,
            infrastructure_retries=len(infrastructure_errors),
            infrastructure_errors=tuple(infrastructure_errors),
        )
