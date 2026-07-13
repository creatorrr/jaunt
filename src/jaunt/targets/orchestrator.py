"""Outer language-target scheduling and report aggregation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

from jaunt.errors import JauntConfigError
from jaunt.targets.base import TargetAdapter, TargetRequest

ReportT = TypeVar("ReportT")


def aggregate_exit_code(codes: Iterable[int]) -> int:
    """Apply Jaunt's mixed-target exit precedence."""

    values = set(codes)
    for code in (2, 3, 4, 5):
        if code in values:
            return code
    return min((code for code in values if code != 0), default=0)


@dataclass(frozen=True, slots=True)
class AggregatedTargetReport(Generic[ReportT]):
    command: str
    targets: Mapping[str, ReportT]
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class TargetOrchestrator:
    """Run independent language adapters under one outer operation."""

    def __init__(self, adapters: Iterable[TargetAdapter]) -> None:
        by_language: dict[str, TargetAdapter] = {}
        for adapter in adapters:
            if adapter.language in by_language:
                raise ValueError(f"duplicate target adapter for {adapter.language!r}")
            by_language[adapter.language] = adapter
        self._adapters = by_language

    def adapters_for(self, languages: Iterable[str] | None = None) -> tuple[TargetAdapter, ...]:
        selected = tuple(languages or self._adapters)
        missing = sorted(set(selected) - set(self._adapters))
        if missing:
            raise JauntConfigError("No configured target for language(s): " + ", ".join(missing))
        return tuple(self._adapters[language] for language in selected)

    async def run(
        self,
        command: str,
        request: TargetRequest,
        operation: Callable[[TargetAdapter, TargetRequest], Awaitable[ReportT]],
        *,
        languages: Iterable[str] | None = None,
    ) -> AggregatedTargetReport[ReportT]:
        adapters = self.adapters_for(languages)
        reports = await asyncio.gather(*(operation(adapter, request) for adapter in adapters))
        targets = {
            adapter.language: report for adapter, report in zip(adapters, reports, strict=True)
        }
        codes = [int(getattr(report, "exit_code", 0)) for report in reports]
        return AggregatedTargetReport(
            command=command,
            targets=targets,
            exit_code=aggregate_exit_code(codes),
        )

    async def build(
        self, request: TargetRequest, *, languages: Iterable[str] | None = None
    ) -> AggregatedTargetReport:
        return await self.run(
            "build", request, lambda adapter, item: adapter.build(item), languages=languages
        )

    async def test(
        self, request: TargetRequest, *, languages: Iterable[str] | None = None
    ) -> AggregatedTargetReport:
        return await self.run(
            "test", request, lambda adapter, item: adapter.test(item), languages=languages
        )

    async def check(
        self, request: TargetRequest, *, languages: Iterable[str] | None = None
    ) -> AggregatedTargetReport:
        return await self.run(
            "check", request, lambda adapter, item: adapter.check(item), languages=languages
        )
