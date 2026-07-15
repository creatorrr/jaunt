"""Shared cache and progress plumbing for language-neutral generation requests."""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable

from jaunt.cache import CacheEntry, ResponseCache
from jaunt.cost import CostTracker
from jaunt.generate.base import (
    GenerationRequest,
    GenerationResult,
    GeneratorBackend,
    TokenUsage,
    generation_request_cache_key,
)

RequestProgress = Callable[[str, str], None]
CacheValidator = Callable[[str], list[str] | Awaitable[list[str]]]


async def _validate(request: GenerationRequest, source: str) -> list[str]:
    result = request.validator(source)
    return await result if inspect.isawaitable(result) else result


async def _validate_with(validator: CacheValidator, source: str) -> list[str]:
    result = validator(source)
    return await result if inspect.isawaitable(result) else result


def store_generation_result(
    response_cache: ResponseCache | None,
    backend: GeneratorBackend,
    request: GenerationRequest,
    result: GenerationResult,
    *,
    generation_fingerprint: str,
) -> None:
    """Persist a result after its caller has completed any outer overlay checks."""

    if response_cache is None or result.source is None or result.errors:
        return
    cache_key = generation_request_cache_key(
        request,
        model=backend.model_name,
        provider=backend.provider_name,
        generation_fingerprint=generation_fingerprint,
    )
    usage = result.usage
    response_cache.put(
        cache_key,
        CacheEntry(
            source=result.source,
            prompt_tokens=usage.prompt_tokens if usage is not None else 0,
            completion_tokens=usage.completion_tokens if usage is not None else 0,
            model=usage.model if usage is not None else backend.model_name,
            provider=usage.provider if usage is not None else backend.provider_name,
            cached_at=time.time(),
        ),
    )


async def generate_request_cached(
    backend: GeneratorBackend,
    request: GenerationRequest,
    *,
    max_attempts: int,
    generation_fingerprint: str,
    response_cache: ResponseCache | None = None,
    cost_tracker: CostTracker | None = None,
    progress: RequestProgress | None = None,
    usage_callback: Callable[[TokenUsage], None] | None = None,
    usage_label: str | None = None,
    cached_validator: CacheValidator | None = None,
    store: bool = True,
) -> GenerationResult:
    """Generate one request, accepting and storing only validated response bytes.

    The cache key is deliberately computed by the language-neutral request helper:
    language, model/provider, exact request inputs, and the caller's runtime
    fingerprint all participate. A cached candidate is passed through the request's
    current validator before it can be returned. This is particularly important for
    TypeScript, whose validator is an analyzer overlay rather than a text-only check.
    """

    cache_key: str | None = None
    if response_cache is not None:
        cache_key = generation_request_cache_key(
            request,
            model=backend.model_name,
            provider=backend.provider_name,
            generation_fingerprint=generation_fingerprint,
        )
        cached = response_cache.get(cache_key)
        if cached is not None:
            errors = await (
                _validate_with(cached_validator, cached.source)
                if cached_validator is not None
                else _validate(request, cached.source)
            )
            if not errors:
                if progress is not None:
                    progress("cache hit", "validated")
                if cost_tracker is not None:
                    cost_tracker.record_cache_hit()
                return GenerationResult(
                    attempts=0,
                    source=cached.source,
                    errors=[],
                    usage=None,
                )
            if progress is not None:
                progress("cache rejected", errors[0] if errors else "validation failed")

    attempt_usage_callback = usage_callback
    if attempt_usage_callback is None and cost_tracker is not None:

        def record_usage(usage: TokenUsage) -> None:
            cost_tracker.record(usage_label or request.target_path, usage)
            cost_tracker.check_budget()

        attempt_usage_callback = record_usage

    result = await backend.generate_request_with_retry(
        request,
        max_attempts=max_attempts,
        progress=progress,
        usage_callback=attempt_usage_callback,
    )
    if result.source is None or result.errors:
        return result

    # ``generate_request_with_retry`` validates each attempt using the request's
    # validator. Callers with a larger multi-file overlay can defer persistence until
    # that outer transaction passes by setting ``store=False``.
    if store:
        store_generation_result(
            response_cache,
            backend,
            request,
            result,
            generation_fingerprint=generation_fingerprint,
        )
    return result


__all__ = [
    "CacheValidator",
    "RequestProgress",
    "generate_request_cached",
    "store_generation_result",
]
