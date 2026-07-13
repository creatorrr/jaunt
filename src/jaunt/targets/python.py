"""Compatibility adapter around the existing Python target services."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar, cast

from jaunt.targets.base import (
    Language,
    TargetArtifact,
    TargetBuildReport,
    TargetCheckReport,
    TargetRequest,
    TargetStatus,
    TargetTestReport,
    TargetWorkspace,
)

ResultT = TypeVar("ResultT")
Service = Callable[[TargetRequest], ResultT | Awaitable[ResultT]]


async def _call(service: Service[ResultT], request: TargetRequest) -> ResultT:
    result = service(request)
    if inspect.isawaitable(result):
        return await result
    return cast(ResultT, result)


@dataclass(frozen=True, slots=True)
class PythonTargetServices:
    """Injected seams extracted from the existing CLI command bodies."""

    discover: Service[TargetWorkspace]
    status: Service[TargetStatus]
    build: Service[TargetBuildReport]
    test: Service[TargetTestReport]
    check: Service[TargetCheckReport]
    find_orphans: Service[tuple[TargetArtifact, ...]]


class PythonTargetAdapter:
    """Expose current Python behavior through the target-neutral protocol.

    Services are injected deliberately: this layer does not duplicate Python
    discovery/build logic or import the CLI renderer.
    """

    language: Language = "py"

    def __init__(self, services: PythonTargetServices) -> None:
        self._services = services

    async def discover(self, request: TargetRequest) -> TargetWorkspace:
        return await _call(self._services.discover, request)

    async def status(self, request: TargetRequest) -> TargetStatus:
        return await _call(self._services.status, request)

    async def build(self, request: TargetRequest) -> TargetBuildReport:
        return await _call(self._services.build, request)

    async def test(self, request: TargetRequest) -> TargetTestReport:
        return await _call(self._services.test, request)

    async def check(self, request: TargetRequest) -> TargetCheckReport:
        return await _call(self._services.check, request)

    async def find_orphans(self, request: TargetRequest) -> tuple[TargetArtifact, ...]:
        return await _call(self._services.find_orphans, request)
