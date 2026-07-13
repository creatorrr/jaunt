"""Shared model-call runtime for mixed-language commands.

Python and TypeScript keep independent schedulers so discovery, validation, and
artifact work can overlap.  Model calls are different: one command-level gate
and budget cover both schedulers, preventing a mixed command from silently
turning ``--jobs N`` or one configured cost ceiling into two independent
allowances.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from jaunt.cost import CostTracker, _estimate_cost
from jaunt.errors import JauntGenerationError
from jaunt.generate.base import (
    GenerationModuleResult,
    GenerationRequest,
    GeneratorBackend,
    ModuleSpecContext,
    TokenUsage,
)
from jaunt.targets.base import Language

ResultT = TypeVar("ResultT")


class _SharedBudgetLedger:
    def __init__(self, max_cost: float | None, *, on_exceeded: Callable[[], None]) -> None:
        self.max_cost = max_cost
        self._on_exceeded = on_exceeded
        self._lock = threading.Lock()
        self._records: list[tuple[Language, str, TokenUsage]] = []
        self._cache_hits: dict[Language, int] = {"py": 0, "ts": 0}

    def record(self, language: Language, name: str, usage: TokenUsage) -> None:
        with self._lock:
            self._records.append((language, name, usage))
            estimated = self._estimated_cost_unlocked()
            exceeded = self.max_cost is not None and estimated > self.max_cost
        if exceeded:
            # Stop active and queued model calls before surfacing the budget
            # error. This makes "Aborting" command-wide instead of allowing
            # the sibling language scheduler to keep spending.
            self._on_exceeded()
            assert self.max_cost is not None
            raise JauntGenerationError(
                f"Combined mixed-target cost ${estimated:.4f} exceeds budget "
                f"limit ${self.max_cost:.4f}. Aborting."
            )

    def record_cache_hit(self, language: Language) -> None:
        with self._lock:
            self._cache_hits[language] += 1

    def check_budget(self) -> None:
        with self._lock:
            estimated = self._estimated_cost_unlocked()
            if self.max_cost is not None and estimated > self.max_cost:
                raise JauntGenerationError(
                    f"Combined mixed-target cost ${estimated:.4f} exceeds budget "
                    f"limit ${self.max_cost:.4f}. Aborting."
                )

    def summary(self, language: Language | None = None) -> dict[str, object]:
        with self._lock:
            records = [
                usage
                for record_language, _name, usage in self._records
                if language is None or record_language == language
            ]
            cache_hits = (
                sum(self._cache_hits.values()) if language is None else self._cache_hits[language]
            )
        prompt = sum(item.prompt_tokens for item in records)
        completion = sum(item.completion_tokens for item in records)
        cached = sum(item.cached_prompt_tokens for item in records)
        estimated = sum(
            _estimate_cost(item.model, item.prompt_tokens, item.completion_tokens)
            for item in records
        )
        return {
            "api_calls": len(records),
            "cache_hits": cache_hits,
            "prompt_tokens": prompt,
            "cached_prompt_tokens": cached,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "estimated_cost_usd": round(estimated, 6),
        }

    def _estimated_cost_unlocked(self) -> float:
        return sum(
            _estimate_cost(usage.model, usage.prompt_tokens, usage.completion_tokens)
            for _language, _name, usage in self._records
        )


class SharedCostTracker(CostTracker):
    """A per-phase view whose records charge one command-level ledger."""

    def __init__(self, ledger: _SharedBudgetLedger, language: Language) -> None:
        self._ledger = ledger
        self._language = language
        self._lock = threading.Lock()
        self._local_records: list[tuple[str, TokenUsage]] = []
        self._local_cache_hits = 0

    @property
    def max_cost(self) -> float | None:
        return self._ledger.max_cost

    def child(self) -> SharedCostTracker:
        """Return another phase-local view over the same budget."""

        return SharedCostTracker(self._ledger, self._language)

    def record(self, module_name: str, usage: TokenUsage) -> None:
        # Charge the shared ledger first.  If this crosses the ceiling the
        # command aborts immediately and no later scheduler wave is launched.
        self._ledger.record(self._language, module_name, usage)
        with self._lock:
            self._local_records.append((module_name, usage))

    def record_cache_hit(self) -> None:
        self._ledger.record_cache_hit(self._language)
        with self._lock:
            self._local_cache_hits += 1

    @property
    def total_prompt_tokens(self) -> int:
        with self._lock:
            return sum(usage.prompt_tokens for _, usage in self._local_records)

    @property
    def total_completion_tokens(self) -> int:
        with self._lock:
            return sum(usage.completion_tokens for _, usage in self._local_records)

    @property
    def total_cached_prompt_tokens(self) -> int:
        with self._lock:
            return sum(usage.cached_prompt_tokens for _, usage in self._local_records)

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens

    @property
    def estimated_cost(self) -> float:
        with self._lock:
            return sum(
                _estimate_cost(usage.model, usage.prompt_tokens, usage.completion_tokens)
                for _, usage in self._local_records
            )

    @property
    def cache_hits(self) -> int:
        with self._lock:
            return self._local_cache_hits

    @property
    def api_calls(self) -> int:
        with self._lock:
            return len(self._local_records)

    def check_budget(self) -> None:
        self._ledger.check_budget()


class _ConcurrencyGate:
    """A cancellation-safe semaphore shared by every event loop/thread."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("mixed-target jobs must be >= 1")
        self._semaphore = threading.BoundedSemaphore(limit)
        self._cancelled = threading.Event()
        self._tasks_lock = threading.Lock()
        self._tasks: set[tuple[asyncio.AbstractEventLoop, asyncio.Task[Any]]] = set()

    def cancel(self) -> None:
        """Cancel active/waiting calls, including calls owned by another loop."""

        self._cancelled.set()
        with self._tasks_lock:
            tasks = tuple(self._tasks)
        for loop, task in tasks:
            if task.done() or loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                continue

    async def run(
        self,
        call: Callable[..., Awaitable[ResultT]],
        *args: Any,
        **kwargs: Any,
    ) -> ResultT:
        if self._cancelled.is_set():
            raise asyncio.CancelledError
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio always owns awaited calls
            raise RuntimeError("mixed-target model call has no owning asyncio task")
        identity = (loop, task)
        with self._tasks_lock:
            self._tasks.add(identity)
        if self._cancelled.is_set():
            with self._tasks_lock:
                self._tasks.discard(identity)
            raise asyncio.CancelledError
        acquire = asyncio.create_task(asyncio.to_thread(self._semaphore.acquire))
        try:
            try:
                await asyncio.shield(acquire)
            except asyncio.CancelledError:
                # ``to_thread`` itself cannot be stopped.  Arrange to return a
                # slot if it is acquired after the owner was cancelled.
                def release_after_acquire(done: asyncio.Future[bool]) -> None:
                    if not done.cancelled() and done.exception() is None and done.result():
                        self._semaphore.release()

                acquire.add_done_callback(release_after_acquire)
                raise
            try:
                if self._cancelled.is_set():
                    raise asyncio.CancelledError
                return await call(*args, **kwargs)
            finally:
                self._semaphore.release()
        finally:
            with self._tasks_lock:
                self._tasks.discard(identity)


class LimitedGeneratorBackend(GeneratorBackend):
    """Delegate backend calls through a process-wide mixed-command gate."""

    def __init__(self, delegate: GeneratorBackend, gate: _ConcurrencyGate) -> None:
        self._delegate = delegate
        self._gate = gate

    @property
    def supports_structured_output(self) -> bool:
        return self._delegate.supports_structured_output

    @property
    def model_name(self) -> str:
        return self._delegate.model_name

    @property
    def provider_name(self) -> str:
        return self._delegate.provider_name

    def generation_fingerprint(self, ctx: ModuleSpecContext) -> str:
        return self._delegate.generation_fingerprint(ctx)

    async def generate_module(
        self,
        ctx: ModuleSpecContext,
        *,
        extra_error_context: list[str] | None = None,
    ) -> GenerationModuleResult:
        return await self._gate.run(
            self._delegate.generate_module,
            ctx,
            extra_error_context=extra_error_context,
        )

    async def generate_request(
        self,
        request: GenerationRequest,
        *,
        extra_error_context: list[str] | None = None,
    ) -> GenerationModuleResult:
        return await self._gate.run(
            self._delegate.generate_request,
            request,
            extra_error_context=extra_error_context,
        )

    async def complete_text(self, *, system: str, user: str) -> str:
        return await self._gate.run(self._delegate.complete_text, system=system, user=user)

    async def complete_text_with_usage(
        self, *, system: str, user: str
    ) -> tuple[str, TokenUsage | None]:
        return await self._gate.run(
            self._delegate.complete_text_with_usage,
            system=system,
            user=user,
        )


class MixedTargetRuntime:
    """One concurrency and budget boundary for a mixed CLI operation."""

    def __init__(self, *, jobs: int, max_cost: float | None) -> None:
        self.jobs = jobs
        self._gate = _ConcurrencyGate(jobs)
        self._backend_lock = threading.Lock()
        self._backends: dict[Language, LimitedGeneratorBackend] = {}
        self._operation_lock = threading.Lock()
        self._operations: set[tuple[asyncio.AbstractEventLoop, asyncio.Task[Any]]] = set()
        self._ledger = _SharedBudgetLedger(max_cost, on_exceeded=self.cancel)

    def backend(
        self,
        language: Language,
        factory: Callable[[], GeneratorBackend],
    ) -> LimitedGeneratorBackend:
        with self._backend_lock:
            backend = self._backends.get(language)
            if backend is None:
                backend = LimitedGeneratorBackend(factory(), self._gate)
                self._backends[language] = backend
            return backend

    def cost_tracker(self, language: Language) -> SharedCostTracker:
        return SharedCostTracker(self._ledger, language)

    def summary(self, language: Language | None = None) -> dict[str, object]:
        return self._ledger.summary(language)

    async def run_call(
        self,
        call: Callable[..., Awaitable[ResultT]],
        *args: Any,
        **kwargs: Any,
    ) -> ResultT:
        """Gate a direct model call such as the semantic judge."""

        return await self._gate.run(call, *args, **kwargs)

    def cancel(self) -> None:
        """Cancel model work on every loop after command cancellation/signal."""

        self._gate.cancel()
        with self._operation_lock:
            operations = tuple(self._operations)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for loop, task in operations:
            if task is current or task.done() or loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                continue

    async def run_operation(self, operation: Awaitable[ResultT]) -> ResultT:
        """Register one language operation for command-wide fail-fast cancellation."""

        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is None:  # pragma: no cover
            raise RuntimeError("mixed-target operation has no owning asyncio task")
        identity = (loop, task)
        with self._operation_lock:
            self._operations.add(identity)
        try:
            return await operation
        finally:
            with self._operation_lock:
                self._operations.discard(identity)


__all__ = [
    "LimitedGeneratorBackend",
    "MixedTargetRuntime",
    "SharedCostTracker",
]
